"""
AWS Credits Notifier
====================

Detects two things and notifies on them:

  1. NEW_CREDIT      - a credit ID that has not been seen before
  2. EXPIRING         - a credit crossing an expiry threshold (default 90/60/30/7 days)

State is held in DynamoDB so that:

  * "new" genuinely means new, rather than "present in the latest snapshot"
  * each expiry threshold fires ONCE per credit, instead of every single run

The same handler serves both deployment models:

  * Solution 1 (single account) - TARGET_ACCOUNTS is empty, so the function reads
    credits for the account it runs in using its own execution role.
  * Solution 2 (cross-account)  - TARGET_ACCOUNTS lists payer accounts and
    ASSUME_ROLE_NAME is set, so the function assumes a role into each payer.

Environment variables
---------------------
STATE_TABLE          (required) DynamoDB table name
SNS_TOPIC_ARN        (required) SNS topic to publish to
EXPIRY_THRESHOLDS    comma-separated days, default "90,60,30,7"
TARGET_ACCOUNTS      comma-separated payer account IDs. Empty = current account.
ASSUME_ROLE_NAME     role name to assume in each target account (Solution 2)
EXTERNAL_ID          optional sts:ExternalId required by the spoke role trust policy
MIN_REMAINING_ALERT  skip expiry alerts below this remaining balance, default "0"
STATE_TTL_DAYS       days to retain state rows after a credit expires, default "400"
BILLING_REGION       region to sign the billing call for, default "us-east-1"

No boto3 layer required
-----------------------
GetCredits is signed by hand with botocore's SigV4Auth rather than called through
boto3.client("billing"). botocore is present in every Lambda runtime, and signing
needs no service model, so this works even on runtimes whose bundled boto3 has
never heard of the billing service.
"""

import decimal
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import ReadOnlyCredentials
from botocore.exceptions import ClientError
from botocore.httpsession import URLLib3Session

log = logging.getLogger()
log.setLevel(logging.INFO)

# GetCredits requires startDate to be a past date no more than one year back.
# 364 days keeps us inside that window with a day of slack for clock skew.
LOOKBACK_DAYS = 364

STATE_TABLE = os.environ["STATE_TABLE"]
SNS_TOPIC_ARN = os.environ["SNS_TOPIC_ARN"]
EXPIRY_THRESHOLDS = sorted(
    (int(d.strip()) for d in os.environ.get("EXPIRY_THRESHOLDS", "90,60,30,7").split(",") if d.strip()),
    reverse=True,
)
TARGET_ACCOUNTS = [a.strip() for a in os.environ.get("TARGET_ACCOUNTS", "").split(",") if a.strip()]
ASSUME_ROLE_NAME = os.environ.get("ASSUME_ROLE_NAME", "").strip()
EXTERNAL_ID = os.environ.get("EXTERNAL_ID", "").strip()
MIN_REMAINING_ALERT = decimal.Decimal(os.environ.get("MIN_REMAINING_ALERT", "0"))
STATE_TTL_DAYS = int(os.environ.get("STATE_TTL_DAYS", "400"))
# Billing is effectively a global service homed in us-east-1. Keeping it separate
# from the Lambda's own region means the stack can be deployed anywhere.
BILLING_REGION = os.environ.get("BILLING_REGION", "us-east-1").strip() or "us-east-1"

ddb = boto3.resource("dynamodb")
table = ddb.Table(STATE_TABLE)
sns = boto3.client("sns")
sts = boto3.client("sts")


# ---------------------------------------------------------------- helpers


def _money(amount_obj):
    """CreditData amounts are {currencyAmount, currencyCode}. Return Decimal."""
    if not amount_obj:
        return decimal.Decimal("0")
    raw = amount_obj.get("currencyAmount", "0")
    try:
        return decimal.Decimal(str(raw))
    except (decimal.InvalidOperation, TypeError):
        return decimal.Decimal("0")


def _currency(amount_obj, default="USD"):
    return (amount_obj or {}).get("currencyCode") or default


def _fmt(amount, currency="USD"):
    return f"{currency} {amount:,.2f}"


def _to_epoch(value):
    """
    Normalise a GetCredits timestamp to epoch seconds.

    startDate/endDate/exhaustDate are modelled as `timestamp` shapes, so boto3
    hands back timezone-aware `datetime` objects - NOT the epoch integers the
    wire format uses. Anything that treats these as numbers breaks on the first
    real call, so every read goes through here.

    Also tolerates the other representations this field turns up in, so a
    different boto3 version or a hand-built payload cannot silently produce
    "unknown" dates and suppress every expiry alert:

      * epoch int/float          - the raw wire value
      * "1740000000"             - numeric string
      * "2026-05-09T07:00:00+00:00" / "...Z" - ISO 8601, which is how the AWS
                                   CLI renders it in JSON output
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, (int, float)):
        return int(value)

    text = str(value).strip()
    try:
        return int(float(text))          # numeric string
    except (TypeError, ValueError):
        pass
    try:
        # fromisoformat handles "+00:00"; normalise a trailing Z for older Pythons.
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        log.warning("Unparseable timestamp %r", value)
        return None


def _epoch_to_date(value):
    epoch = _to_epoch(value)
    if epoch is None:
        return "unknown"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _days_until(value, now_epoch):
    epoch = _to_epoch(value)
    if epoch is None:
        return None
    return int((epoch - now_epoch) // 86400)


_own_account_cache = None


def _own_account_id():
    global _own_account_cache
    if _own_account_cache is None:
        _own_account_cache = sts.get_caller_identity()["Account"]
    return _own_account_cache


def _billing_credentials(account_id):
    """
    Frozen credentials to sign the GetCredits call with.

    Uses the execution role for our own account, or assumes the spoke role for a
    different one. Returning credentials rather than a client is what lets us
    sign the request ourselves - see _get_credits.
    """
    own = _own_account_id()

    # A hub may legitimately monitor its own account. Assuming a role into
    # yourself would need a pointless self-trusting role, so use the execution
    # role directly and let the spoke stack be skipped for this account.
    if not ASSUME_ROLE_NAME or account_id == own:
        if ASSUME_ROLE_NAME:
            log.info("Account %s is this account - no AssumeRole", account_id)
        return boto3.Session().get_credentials().get_frozen_credentials()

    role_arn = f"arn:aws:iam::{account_id}:role/{ASSUME_ROLE_NAME}"
    assume_args = {"RoleArn": role_arn, "RoleSessionName": "credit-notifier"}
    if EXTERNAL_ID:
        assume_args["ExternalId"] = EXTERNAL_ID
    c = sts.assume_role(**assume_args)["Credentials"]
    return ReadOnlyCredentials(c["AccessKeyId"], c["SecretAccessKey"], c["SessionToken"])


def _get_credits(account_id, start_epoch, credentials):
    """
    Call billing:GetCredits with a request we sign ourselves.

    WHY NOT boto3.client("billing").get_credits()? Because GetCredits is a recent
    addition and the boto3 bundled with Lambda runtimes usually predates it, which
    fails with UnknownServiceError and previously forced every deployment to ship
    a boto3 layer. SigV4Auth needs no service model, so signing the request by
    hand works on any runtime and removes that dependency entirely.

    Verified against botocore 1.43.39, which has no billing service model at all.
    """
    body = json.dumps(
        {
            "accountId": account_id,
            "startDate": start_epoch,          # JSON 1.0 wire format: epoch seconds
            "payerAccountFlag": True,          # aggregate the whole billing family
        }
    )
    request = AWSRequest(
        method="POST",
        url=f"https://billing.{BILLING_REGION}.api.aws/",
        data=body,
        headers={
            "Content-Type": "application/x-amz-json-1.0",
            "X-Amz-Target": "AWSBilling.GetCredits",
        },
    )
    SigV4Auth(credentials, "billing", BILLING_REGION).add_auth(request)
    response = URLLib3Session().send(request.prepare())

    payload = {}
    if response.content:
        try:
            payload = json.loads(response.content.decode())
        except ValueError:
            payload = {"message": response.content[:300].decode(errors="replace")}

    if response.status_code != 200:
        kind = str(payload.get("__type", "")).split("#")[-1] or f"HTTP{response.status_code}"
        raise RuntimeError(f"{kind}: {payload.get('message') or payload.get('Message') or payload}")

    # GetCredits has no nextToken - the response is a single complete list.
    return payload.get("credits", [])


def _fetch_credits(account_id, now_epoch):
    """Read credits for one account, aggregating its consolidated billing family."""
    start_epoch = now_epoch - (LOOKBACK_DAYS * 86400)
    return _get_credits(account_id, start_epoch, _billing_credentials(account_id))


def _expand_simulated(entries, account_id, now_epoch):
    """
    Turn a compact test payload into full GetCredits-shaped credits.

    Real credits with a future expiry are hard to come by, which makes the
    EXPIRING path awkward to prove against a live deployment. Passing credits in
    the invocation event exercises every downstream step - state, de-duplication,
    thresholds, rendering, SNS - using the deployed function and real emails.

    "endInDays" is the useful part: it saves computing epochs by hand.

        {"creditId": "TEST-1", "endInDays": 45, "remaining": "500.00"}

    This is driven by the EVENT, never by configuration, so a scheduled run
    cannot trigger it. EventBridge Scheduler sends no such key.
    """
    out = []
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            continue
        amount = str(e.get("amount", "1000.00"))
        remaining = str(e.get("remaining", amount))
        credit = {
            "creditId": e.get("creditId") or f"SIMULATED-{i + 1}",
            "accountId": e.get("accountId") or account_id,
            "creditType": e.get("creditType", "Promotion"),
            "creditStatus": e.get("creditStatus", "ENABLED"),
            "description": e.get("description") or "SIMULATED test credit",
            "initialAmount": {"currencyCode": "USD", "currencyAmount": amount},
            "remainingAmount": {"currencyCode": "USD", "currencyAmount": remaining},
            "applicableProductNames": e.get("applicableProductNames") or [],
        }
        if "startDate" in e:
            credit["startDate"] = e["startDate"]
        else:
            credit["startDate"] = now_epoch + int(e.get("startInDays", -30)) * 86400
        # Omitting both leaves the credit with no expiry, which is a valid case.
        if "endDate" in e:
            credit["endDate"] = e["endDate"]
        elif "endInDays" in e:
            credit["endDate"] = now_epoch + int(e["endInDays"]) * 86400
        out.append(credit)
    return out


def _state_key(account_id, credit_id):
    """
    Partition on account + credit rather than credit alone. In the hub-and-spoke
    model several payer accounts are scanned by one function, and scoping the key
    guarantees one payer's state can never overwrite another's.
    """
    return f"{account_id}#{credit_id}"


def _baseline_key(account_id):
    """Marker row proving this account has been scanned at least once."""
    return f"__baseline__#{account_id}"


def _has_baseline(account_id):
    return _load_state(_baseline_key(account_id)) is not None


def _write_baseline(account_id, credit_count):
    table.put_item(
        Item={
            "creditKey": _baseline_key(account_id),
            "accountId": account_id,
            "recordType": "BASELINE_MARKER",
            "creditsAtBaseline": credit_count,
            "establishedAt": int(time.time()),
        }
    )


def _load_state(credit_key):
    try:
        return table.get_item(Key={"creditKey": credit_key}).get("Item")
    except ClientError:
        log.exception("Failed reading state for %s", credit_key)
        return None


def _save_state(credit_key, credit_id, account_id, end_date, fired_thresholds, remaining):
    end_epoch = _to_epoch(end_date) or 0
    # A credit with no end date must not get a TTL in the past, which would make
    # DynamoDB delete the row and the credit look brand new on the next run.
    expire_at = (end_epoch or int(time.time())) + (STATE_TTL_DAYS * 86400)
    table.put_item(
        Item={
            "creditKey": credit_key,
            "creditId": credit_id,
            "accountId": account_id,
            "endDate": end_epoch,
            "firedThresholds": sorted(int(t) for t in fired_thresholds),
            "lastRemaining": remaining,
            "lastSeen": int(time.time()),
            "expireAt": expire_at,  # DynamoDB TTL attribute
        }
    )


# ---------------------------------------------------------------- detection


def _evaluate(credit, account_id, now_epoch):
    """
    Compare one credit against stored state.
    Returns (list_of_alerts, thresholds_to_persist, credit_key).
    """
    credit_id = credit.get("creditId")
    if not credit_id:
        log.warning("Skipping credit with no creditId in account %s", account_id)
        return [], set(), None

    end_date = credit.get("endDate")
    remaining_obj = credit.get("remainingAmount") or credit.get("estimatedAmount")
    remaining = _money(remaining_obj)
    currency = _currency(remaining_obj)

    owning_account = credit.get("accountId") or account_id
    credit_key = _state_key(owning_account, credit_id)

    prior = _load_state(credit_key)
    fired = set(int(t) for t in (prior or {}).get("firedThresholds", []))
    alerts = []

    days_left = _days_until(end_date, now_epoch)
    already_expired = days_left is not None and days_left < 0

    common = {
        "creditKey": credit_key,
        "creditId": credit_id,
        "accountId": owning_account,
        "description": credit.get("description") or credit.get("creditType") or "(no description)",
        "creditType": credit.get("creditType"),
        "creditStatus": credit.get("creditStatus"),
        "initial": _fmt(_money(credit.get("initialAmount")), _currency(credit.get("initialAmount"))),
        "remaining": _fmt(remaining, currency),
        "startDate": _epoch_to_date(credit.get("startDate")),
        "expiryDate": _epoch_to_date(end_date),
        "daysRemaining": days_left,
        "applicableProducts": credit.get("applicableProductNames") or [],
    }

    # ---- 1. brand new credit
    #
    # "First time we have seen it" is NOT the same as "newly assigned". A credit
    # that already expired can appear unseen for several reasons - a payer account
    # added to TARGET_ACCOUNTS, a rebuilt state table, a TTL-expired row - and
    # announcing a months-dead credit as new is worse than saying nothing.
    # It is still recorded, so it cannot resurface as new later.
    if prior is None:
        if already_expired:
            log.info(
                "First sight of credit %s but it expired on %s (%d days ago) - "
                "recording without alerting",
                credit_id, common["expiryDate"], abs(days_left),
            )
        else:
            alerts.append({**common, "alertType": "NEW_CREDIT"})

    # ---- 2. expiry thresholds
    if days_left is not None and days_left >= 0:
        if remaining >= MIN_REMAINING_ALERT:
            # Fire the tightest threshold crossed that has not fired yet, so a
            # credit discovered at 12 days out alerts once at 30, not at 90+60+30.
            crossed = [t for t in EXPIRY_THRESHOLDS if days_left <= t and t not in fired]
            if crossed:
                threshold = min(crossed)
                alerts.append(
                    {
                        **common,
                        "alertType": "EXPIRING",
                        "threshold": threshold,
                        "daysRemaining": days_left,
                    }
                )
                # Mark every crossed threshold as fired to suppress the wider ones.
                fired.update(crossed)
        else:
            log.info(
                "Credit %s within %sd of expiry but remaining %s is below MIN_REMAINING_ALERT",
                credit_id, days_left, remaining,
            )

    return alerts, fired, credit_key


# ---------------------------------------------------------------- messaging


def _expiry_suffix(days_left):
    """Spell out the remaining lifetime so a date alone can never mislead."""
    if days_left is None:
        return "  (no expiry date)"
    if days_left < 0:
        return f"  (EXPIRED {abs(days_left)} days ago)"
    if days_left == 0:
        return "  (expires TODAY)"
    return f"  (in {days_left} days)"


def _render(alert):
    if alert["alertType"] == "NEW_CREDIT":
        subject = f"[AWS Credits] New credit assigned - {alert['accountId']}"
        days = alert.get("daysRemaining")
        if days is None:
            headline = ("A new AWS credit has been assigned to your account. "
                        "It has no expiry date.")
        else:
            headline = (
                f"A new AWS credit has been assigned to your account. "
                f"You have {days} days to use it."
            )
    else:
        subject = (
            f"[AWS Credits] Expiring in {alert['daysRemaining']} days - {alert['accountId']}"
        )
        headline = (
            f"An AWS credit expires in {alert['daysRemaining']} days "
            f"({alert['threshold']}-day notice) with {alert['remaining']} still unused."
        )

    products = ", ".join(alert["applicableProducts"]) or "All eligible services"
    body = "\n".join(
        [
            headline,
            "",
            f"Account            : {alert['accountId']}",
            f"Credit ID          : {alert['creditId']}",
            f"Description        : {alert['description']}",
            f"Type               : {alert['creditType']}",
            f"Status             : {alert['creditStatus']}",
            f"Original amount    : {alert['initial']}",
            f"Remaining          : {alert['remaining']}",
            f"Effective from     : {alert['startDate']}",
            f"Expires on         : {alert['expiryDate']}{_expiry_suffix(alert.get('daysRemaining'))}",
            f"Applies to         : {products}",
            "",
            "View details in the AWS Billing and Cost Management console:",
            "https://console.aws.amazon.com/billing/home#/credits",
        ]
    )
    return subject, body


def _render_baseline(account_id, rows):
    """
    First-ever scan of an account. Every credit would otherwise look brand new,
    so send one inventory email instead of a burst of NEW_CREDIT alerts.
    """
    subject = f"[AWS Credits] Monitoring enabled for {account_id} - {len(rows)} credit(s) found"

    if not rows:
        body_rows = ["  (no active credits found)"]
    else:
        # Soonest expiry first - that is the actionable end of the list.
        rows = sorted(rows, key=lambda r: r["daysLeft"] if r["daysLeft"] is not None else 10**9)
        body_rows = []
        for r in rows:
            when = (
                f"{r['daysLeft']} days"
                if r["daysLeft"] is not None and r["daysLeft"] >= 0
                else "expired"
            )
            body_rows.append(
                f"  - {r['description']}\n"
                f"      remaining {r['remaining']} | expires {r['expiryDate']} ({when})"
            )

    body = "\n".join(
        [
            f"Credit monitoring is now active for account {account_id}.",
            "",
            "This is the initial inventory, sent once. From now on you will be",
            "notified individually when a new credit is assigned and as each",
            "credit approaches its expiry date.",
            "",
            f"Credits currently visible ({len(rows)}):",
            "",
            *body_rows,
            "",
            "View details in the AWS Billing and Cost Management console:",
            "https://console.aws.amazon.com/billing/home#/credits",
        ]
    )

    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],
        Message=body,
        MessageAttributes={
            "alertType": {"DataType": "String", "StringValue": "BASELINE"},
            "accountId": {"DataType": "String", "StringValue": str(account_id)},
        },
    )


def _publish(alert):
    subject, body = _render(alert)
    sns.publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],  # SNS subject hard limit
        Message=body,
        MessageAttributes={
            "alertType": {"DataType": "String", "StringValue": alert["alertType"]},
            "accountId": {"DataType": "String", "StringValue": str(alert["accountId"])},
        },
    )


# ---------------------------------------------------------------- entrypoint


def handler(event, context):
    now_epoch = int(time.time())
    accounts = TARGET_ACCOUNTS or [sts.get_caller_identity()["Account"]]

    simulated = (event or {}).get("simulateCredits") if isinstance(event, dict) else None
    if simulated is not None:
        log.warning(
            "SIMULATION MODE: processing %d credit(s) from the invocation event. "
            "GetCredits will NOT be called. Alerts and state writes are real.",
            len(simulated),
        )
        accounts = accounts[:1]

    summary = {"accountsScanned": 0, "creditsSeen": 0, "alertsSent": 0, "failures": []}
    if simulated is not None:
        summary["simulated"] = True

    for account_id in accounts:
        try:
            if simulated is not None:
                credits = _expand_simulated(simulated, account_id, now_epoch)
            else:
                credits = _fetch_credits(account_id, now_epoch)
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            log.exception("Reading credits failed for %s", account_id)
            summary["failures"].append({"accountId": account_id, "error": str(exc)})
            continue

        summary["accountsScanned"] += 1
        summary["creditsSeen"] += len(credits)
        log.info("Account %s returned %d credit(s)", account_id, len(credits))

        # First ever scan of this account: record everything silently and send a
        # single inventory email, rather than one NEW_CREDIT alert per credit.
        # Also covers a payer account added to an existing hub deployment later.
        is_baseline = not _has_baseline(account_id)
        baseline_rows = []

        for credit in credits:
            try:
                alerts, fired, credit_key = _evaluate(credit, account_id, now_epoch)
                if credit_key is None:
                    continue

                if is_baseline:
                    # Mark every already-crossed threshold as fired so the run
                    # after the baseline does not replay them as fresh alerts.
                    days_left = _days_until(credit.get("endDate"), now_epoch)
                    if days_left is not None:
                        fired.update(t for t in EXPIRY_THRESHOLDS if days_left <= t)
                    remaining_obj = credit.get("remainingAmount") or credit.get("estimatedAmount")
                    baseline_rows.append(
                        {
                            "description": credit.get("description")
                            or credit.get("creditType")
                            or "(no description)",
                            "remaining": _fmt(_money(remaining_obj), _currency(remaining_obj)),
                            "expiryDate": _epoch_to_date(credit.get("endDate")),
                            "daysLeft": days_left,
                        }
                    )
                else:
                    for alert in alerts:
                        _publish(alert)
                        summary["alertsSent"] += 1
                        log.info("Alert %s for credit %s", alert["alertType"], alert["creditId"])

                remaining_obj = credit.get("remainingAmount") or credit.get("estimatedAmount")
                _save_state(
                    credit_key=credit_key,
                    credit_id=credit["creditId"],
                    account_id=credit.get("accountId") or account_id,
                    end_date=credit.get("endDate"),
                    fired_thresholds=fired,
                    remaining=_money(remaining_obj),
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Failed processing credit %s", credit.get("creditId"))
                summary["failures"].append(
                    {"creditId": credit.get("creditId"), "error": str(exc)}
                )

        if is_baseline:
            # Written last so a mid-run failure leaves the account un-baselined
            # and the next invocation retries cleanly instead of going silent.
            _render_baseline(account_id, baseline_rows)
            _write_baseline(account_id, len(baseline_rows))
            summary["alertsSent"] += 1
            summary["baselinedAccounts"] = summary.get("baselinedAccounts", []) + [account_id]
            log.info("Baseline established for %s with %d credit(s)", account_id, len(baseline_rows))

    log.info("Run summary: %s", json.dumps(summary, default=str))

    # Surface partial failures so the Lambda Errors alarm can catch a broken account.
    if summary["failures"] and summary["accountsScanned"] == 0:
        raise RuntimeError(f"All accounts failed: {json.dumps(summary['failures'], default=str)}")

    return summary
