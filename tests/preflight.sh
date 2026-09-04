#!/usr/bin/env bash
#
# Pre-flight check: can this account actually read AWS credits?
#
# Run this BEFORE deploying anything. If it fails, the stack will deploy fine
# and then never produce a single alert.
#
# billing:GetCredits is a recent API. Most installed AWS CLI versions do not
# have it yet (CLI 2.35.11 does not), so this script tries the CLI, then falls
# back to Python, and as a last resort builds a throwaway virtualenv with a
# current boto3. You do not need to work out which path applies.
#
# Usage:
#   ./preflight.sh                       # check the account you are logged into
#   ./preflight.sh -a 111122223333       # check a specific account
#   ./preflight.sh -a 111122223333 -r us-east-1
#   ./preflight.sh --no-bootstrap        # never create a temp venv

set -uo pipefail   # NOTE: deliberately no -e; this script probes and reports.

ACCOUNT=""
REGION="us-east-1"
BOOTSTRAP="yes"

while [ $# -gt 0 ]; do
  case "$1" in
    -a|--account)   ACCOUNT="${2:-}"; shift 2 ;;
    -r|--region)    REGION="${2:-}"; shift 2 ;;
    --no-bootstrap) BOOTSTRAP="no"; shift ;;
    -h|--help)      sed -n '3,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

LOOKBACK=$(( 86400 * 364 ))   # API requires startDate in the past, under 1 year
START=$(( $(date +%s) - LOOKBACK ))

hr() { printf '%s\n' "----------------------------------------------------------------"; }

# --- credentials -------------------------------------------------------------

hr
CALLER="$(aws sts get-caller-identity --output json 2>&1)"
if ! printf '%s' "$CALLER" | grep -q '"Account"'; then
  echo "FAIL  No valid AWS credentials."
  echo "      $(printf '%s' "$CALLER" | tail -1)"
  echo
  echo "      Refresh them, then re-run this script."
  exit 1
fi
SELF="$(printf '%s' "$CALLER" | sed -n 's/.*"Account": *"\([0-9]*\)".*/\1/p')"
[ -n "$ACCOUNT" ] || ACCOUNT="$SELF"

echo "OK    Credentials valid, signed in to account $SELF"
echo "      Checking credits for account $ACCOUNT in $REGION"

if [ "$ACCOUNT" != "$SELF" ]; then
  echo "WARN  You are asking about $ACCOUNT but signed in to $SELF."
  echo "      GetCredits reads the caller's own billing data, so this will"
  echo "      almost certainly fail unless you switch credentials."
fi

# --- is this a management (payer) account? -----------------------------------

hr
ORG="$(aws organizations describe-organization --region "$REGION" --output json 2>&1)"
MASTER="$(printf '%s' "$ORG" | sed -n 's/.*"MasterAccountId": *"\([0-9]*\)".*/\1/p')"
if [ -z "$MASTER" ]; then
  echo "WARN  Could not read AWS Organizations."
  echo "      If this account is not a management (payer) account, the"
  echo "      payerAccountFlag used by this solution has nothing to aggregate."
elif [ "$MASTER" = "$SELF" ]; then
  echo "OK    This IS the management (payer) account - payerAccountFlag is valid"
else
  echo "WARN  This is a MEMBER account. Management account is $MASTER."
  echo "      Credits are normally visible from the payer account, so deploy"
  echo "      there instead, or expect an empty result here."
fi

# --- probe GetCredits --------------------------------------------------------

hr
RESULT=""
METHOD=""

# 1. AWS CLI
if aws billing get-credits --account-id "$ACCOUNT" --start-date "$START" \
     --payer-account-flag --region "$REGION" --output json >/tmp/pf_cli.$$ 2>/tmp/pf_cli_err.$$; then
  RESULT="$(cat /tmp/pf_cli.$$)"; METHOD="AWS CLI"
else
  ERR="$(cat /tmp/pf_cli_err.$$)"
  case "$ERR" in
    *"invalid choice"*|*"Invalid choice"*|*"Unknown options"*|*"argument operation"*)
      echo "SKIP  AWS CLI $(aws --version 2>&1 | awk '{print $1}') has no 'billing get-credits'"
      echo "      (expected - the API is newer than most installed CLIs)" ;;
    *)
      echo "NOTE  AWS CLI call failed:"
      printf '      %s\n' "$(printf '%s' "$ERR" | tail -2)" ;;
  esac
fi
rm -f /tmp/pf_cli.$$ /tmp/pf_cli_err.$$

probe_python() {
  "$1" - "$ACCOUNT" "$START" "$REGION" <<'PY' 2>&1
import sys, json
acct, start, region = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    import boto3, botocore
except ImportError:
    print("NO_BOTO3"); sys.exit(3)
from datetime import datetime, timezone
c = boto3.client("billing", region_name=region)
if not hasattr(c, "get_credits"):
    print("NO_GETCREDITS " + botocore.__version__); sys.exit(4)
try:
    r = c.get_credits(accountId=acct,
                      startDate=datetime.fromtimestamp(start, tz=timezone.utc),
                      payerAccountFlag=True)
except Exception as e:
    print("APIERROR " + type(e).__name__ + ": " + str(e)[:300]); sys.exit(5)
out = []
for x in r.get("credits", []):
    end = x.get("endDate")
    out.append({"description": x.get("description"),
                "remaining": (x.get("remainingAmount") or {}).get("currencyAmount"),
                "currency": (x.get("remainingAmount") or {}).get("currencyCode"),
                "expires": end.strftime("%Y-%m-%d") if end else "no end date"})
print("OKJSON " + json.dumps(out))
PY
}

# 2. system python
if [ -z "$METHOD" ] && command -v python3 >/dev/null 2>&1; then
  OUT="$(probe_python python3)"
  case "$OUT" in
    OKJSON*)        RESULT="${OUT#OKJSON }"; METHOD="python3 + boto3" ;;
    NO_BOTO3)       echo "SKIP  python3 has no boto3 installed" ;;
    NO_GETCREDITS*) echo "SKIP  python3 boto3 is too old (botocore ${OUT#NO_GETCREDITS })" ;;
    APIERROR*)      echo "FAIL  ${OUT#APIERROR }"; METHOD="error" ;;
  esac
fi

# 3. throwaway venv with a current boto3
if [ -z "$METHOD" ] && [ "$BOOTSTRAP" = "yes" ]; then
  echo "INFO  Building a throwaway virtualenv with a current boto3..."
  VENV="$(mktemp -d)"
  if python3 -m venv "$VENV" >/dev/null 2>&1 \
     && "$VENV/bin/pip" install --quiet --upgrade boto3 botocore >/dev/null 2>&1; then
    OUT="$(probe_python "$VENV/bin/python")"
    case "$OUT" in
      OKJSON*)        RESULT="${OUT#OKJSON }"; METHOD="temporary venv (botocore current)" ;;
      APIERROR*)      echo "FAIL  ${OUT#APIERROR }"; METHOD="error" ;;
      NO_GETCREDITS*) echo "FAIL  even a current botocore lacks GetCredits (${OUT#NO_GETCREDITS })"; METHOD="error" ;;
      *)              echo "FAIL  unexpected: $OUT"; METHOD="error" ;;
    esac
  else
    echo "FAIL  Could not build a virtualenv (no network, or python3-venv missing)."
  fi
  rm -rf "$VENV"
fi

# --- verdict -----------------------------------------------------------------

hr
if [ "$METHOD" = "error" ]; then
  echo "VERDICT: the API is reachable but the call was rejected."
  echo
  echo "  AccessDeniedException  -> the identity lacks billing:GetCredits, or an"
  echo "                            SCP denies billing actions in this account."
  echo "  ValidationException    -> startDate must be in the past, under 1 year."
  exit 1
fi

if [ -z "$METHOD" ]; then
  echo "VERDICT: could not verify. No available tool knows billing:GetCredits."
  echo
  echo "  Fix any ONE of these, then re-run:"
  echo "    pip3 install --upgrade boto3 botocore"
  echo "    upgrade the AWS CLI to a version that has 'aws billing get-credits'"
  exit 1
fi

COUNT="$(printf '%s' "$RESULT" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))' 2>/dev/null || echo "?")"
echo "PASS  GetCredits works. Method: $METHOD"
echo "      Credits visible: $COUNT"
if [ "$COUNT" != "0" ] && [ "$COUNT" != "?" ]; then
  echo
  printf '%s' "$RESULT" | python3 -c '
import json,sys
for c in json.load(sys.stdin):
    print("      - {} | {} {} remaining | expires {}".format(
        (c["description"] or "(no description)")[:52],
        c["currency"] or "", c["remaining"] or "0", c["expires"]))'
fi
echo
if [ "$METHOD" != "AWS CLI" ]; then
  echo "NOTE  Your AWS CLI cannot make this call, but Lambda does not use the CLI."
  echo "      It does mean the Lambda runtime's bundled boto3 is probably also too"
  echo "      old, so deploy with the boto3 layer - publish.sh builds one for you."
fi
echo "Next: ./publish.sh -b YOUR-BUCKET -r $REGION -e you@example.com"
hr
