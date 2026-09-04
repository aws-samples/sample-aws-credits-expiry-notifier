#!/usr/bin/env bash
#
# Exercise both alert paths against a DEPLOYED stack, without needing real
# credits that have a future expiry date.
#
# Credits are supplied in the invocation event, so nothing is reconfigured and
# the daily schedule cannot trigger this. Everything downstream is real: state,
# de-duplication, thresholds, rendering and the emails you receive.
#
# Usage:
#   ./test-alerts.sh -s STACK -r REGION --list        show the steps
#   ./test-alerts.sh -s STACK -r REGION --step 1      run ONE step, then stop
#   ./test-alerts.sh -s STACK -r REGION               run all seven
#   ./test-alerts.sh -s STACK -r REGION --reset       clear simulated state only
#   ./test-alerts.sh -s STACK -r REGION --cleanup     same as --reset
#
# Steps 1-5 follow ONE credit through its life and must run IN ORDER - each
# depends on the state the previous one wrote. Steps 6 and 7 stand alone.
#
# Confirm your SNS email subscription first, or alertsSent will climb while
# nothing arrives.

set -uo pipefail

STACK=""; REGION="us-east-1"; ACCOUNT=""; ACTION="all"; ONE=""

while [ $# -gt 0 ]; do
  case "$1" in
    -s|--stack)   STACK="${2:-}"; shift 2 ;;
    -r|--region)  REGION="${2:-}"; shift 2 ;;
    -a|--account) ACCOUNT="${2:-}"; shift 2 ;;
    --step)       ACTION="one"; ONE="${2:-}"; shift 2 ;;
    --list)       ACTION="list"; shift ;;
    --reset|--cleanup) ACTION="reset"; shift ;;
    -h|--help)    sed -n '3,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$STACK" ] || { echo "ERROR: -s/--stack is required" >&2; exit 2; }

FN="${STACK}-notifier"
TABLE="${STACK}-credit-state"

# ---- the steps -------------------------------------------------------------
# Index 1..7. Kept as parallel indexed arrays so this works on bash 3.2 (macOS).

LABEL[1]="A NEW credit is assigned, 120 days of life left"
EXPECT[1]=1
DETAIL[1]="NEW_CREDIT. Email should read 'You have 120 days to use it'"
PAYLOAD[1]='{"simulateCredits":[{"creditId":"SIMTEST-LIFECYCLE","endInDays":120,"amount":"5000.00","description":"SIMULATED lifecycle credit"}]}'

LABEL[2]="Same credit, now 88 days out - crosses the 90-day threshold"
EXPECT[2]=1
DETAIL[2]="EXPIRING. Subject should say 'Expiring in 88 days'"
PAYLOAD[2]='{"simulateCredits":[{"creditId":"SIMTEST-LIFECYCLE","endInDays":88,"amount":"5000.00","description":"SIMULATED lifecycle credit"}]}'

LABEL[3]="Same credit the next day, 87 days out"
EXPECT[3]=0
DETAIL[3]="SILENT. The 90-day threshold already fired - this is the de-duplication"
PAYLOAD[3]='{"simulateCredits":[{"creditId":"SIMTEST-LIFECYCLE","endInDays":87,"amount":"5000.00","description":"SIMULATED lifecycle credit"}]}'

LABEL[4]="Time passes, now 58 days out - crosses 60"
EXPECT[4]=1
DETAIL[4]="EXPIRING. Subject 'Expiring in 58 days', body says 60-day notice"
PAYLOAD[4]='{"simulateCredits":[{"creditId":"SIMTEST-LIFECYCLE","endInDays":58,"amount":"5000.00","description":"SIMULATED lifecycle credit"}]}'

LABEL[5]="Now 5 days out - crosses BOTH 30 and 7"
EXPECT[5]=1
DETAIL[5]="ONE email only, not two, labelled with the tightest threshold"
PAYLOAD[5]='{"simulateCredits":[{"creditId":"SIMTEST-LIFECYCLE","endInDays":5,"amount":"5000.00","description":"SIMULATED lifecycle credit"}]}'

LABEL[6]="A nearly-spent credit, 10 days out, USD 0.42 left"
EXPECT[6]=2
DETAIL[6]="TWO emails - NEW_CREDIT and EXPIRING. It is new AND inside a threshold. Raise MinRemainingForExpiryAlert above 0.42 to drop the expiry one"
PAYLOAD[6]='{"simulateCredits":[{"creditId":"SIMTEST-DUST","endInDays":10,"amount":"35.00","remaining":"0.42","description":"SIMULATED nearly spent"}]}'

LABEL[7]="A credit first seen AFTER it already expired"
EXPECT[7]=0
DETAIL[7]="SILENT. Recorded, never announced as new"
PAYLOAD[7]='{"simulateCredits":[{"creditId":"SIMTEST-DEAD","endInDays":-116,"amount":"35.00","description":"SIMULATED already expired"}]}'

LAST=7
TEST_IDS="SIMTEST-LIFECYCLE SIMTEST-DUST SIMTEST-DEAD"

# ---- helpers ---------------------------------------------------------------

if [ "$ACTION" = "list" ]; then
  echo "Steps (1-5 are one credit's life and must run in order):"
  echo
  i=1
  while [ "$i" -le "$LAST" ]; do
    printf '  %s. %s\n' "$i" "${LABEL[$i]}"
    printf '       expect %s email(s) - %s\n\n' "${EXPECT[$i]}" "${DETAIL[$i]}"
    i=$((i + 1))
  done
  echo "Run one:  ./test-alerts.sh -s $STACK -r $REGION --step 1"
  exit 0
fi

# Fail fast on bad credentials. Continuing with an empty account ID produces
# misleading output - state lookups silently miss and every step looks broken.
if [ -z "$ACCOUNT" ]; then
  ACCOUNT="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
fi
case "$ACCOUNT" in
  ''|*[!0-9]*)
    echo "ERROR: could not determine the AWS account." >&2
    echo "       Credentials are probably expired - refresh them and re-run," >&2
    echo "       or pass -a <accountId> explicitly." >&2
    exit 1 ;;
esac

if ! aws lambda get-function --function-name "$FN" --region "$REGION" >/dev/null 2>&1; then
  echo "ERROR: function $FN not found in $REGION (account $ACCOUNT)." >&2
  echo "       Check -s/--stack and -r/--region." >&2
  exit 1
fi

purge() {
  for cid in $TEST_IDS; do
    aws dynamodb delete-item --table-name "$TABLE" --region "$REGION" \
      --key "{\"creditKey\":{\"S\":\"${ACCOUNT}#${cid}\"}}" >/dev/null 2>&1
  done
}

if [ "$ACTION" = "reset" ]; then
  echo "Clearing simulated credit state from $TABLE"
  purge
  echo "Done. Real credits are untouched. Step 1 will behave as a fresh credit again."
  exit 0
fi

ensure_baseline() {
  if ! aws dynamodb get-item --table-name "$TABLE" --region "$REGION" \
       --key "{\"creditKey\":{\"S\":\"__baseline__#${ACCOUNT}\"}}" \
       --query 'Item' --output text 2>/dev/null | grep -q .; then
    echo "No baseline marker yet - invoking once so the real inventory email goes"
    echo "first and simulated credits are then judged as changes."
    aws lambda invoke --function-name "$FN" --region "$REGION" /dev/null >/dev/null 2>&1
    sleep 3
    echo
  fi
}

FAILED=0

run_step() {
  n="$1"
  printf '%s\n' "============================================================"
  echo "STEP $n: ${LABEL[$n]}"
  echo
  echo "  expecting ${EXPECT[$n]} email(s)"
  echo "  ${DETAIL[$n]}"
  echo
  tmp="$(mktemp)"
  # Write the response to a real file. /dev/stdout mixes the function's payload
  # with the CLI's own metadata JSON, which then cannot be parsed.
  aws lambda invoke --function-name "$FN" --region "$REGION" \
      --cli-binary-format raw-in-base64-out \
      --payload "${PAYLOAD[$n]}" "$tmp" >/dev/null 2>&1
  sent="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("alertsSent","?"))' "$tmp" 2>/dev/null || echo "?")"
  rm -f "$tmp"

  if [ "$sent" = "${EXPECT[$n]}" ]; then
    echo "  RESULT: alertsSent=$sent   PASS"
  else
    echo "  RESULT: alertsSent=$sent   FAIL - expected ${EXPECT[$n]}"
    FAILED=$((FAILED + 1))
  fi
}

# ---- run -------------------------------------------------------------------

if [ "$ACTION" = "one" ]; then
  case "$ONE" in
    ''|*[!0-9]*) echo "ERROR: --step needs a number 1-$LAST" >&2; exit 2 ;;
  esac
  if [ "$ONE" -lt 1 ] || [ "$ONE" -gt "$LAST" ]; then
    echo "ERROR: --step must be 1-$LAST" >&2; exit 2
  fi

  ensure_baseline
  if [ "$ONE" = "1" ]; then
    echo "Clearing any leftover simulated state so step 1 starts fresh."
    purge
    echo
  fi

  run_step "$ONE"
  echo
  if [ "$ONE" -lt "$LAST" ]; then
    echo "Check your inbox, then run the next one:"
    echo "  ./test-alerts.sh -s $STACK -r $REGION --step $((ONE + 1))"
  else
    echo "That was the last step. Clear the simulated state when you are done:"
    echo "  ./test-alerts.sh -s $STACK -r $REGION --reset"
  fi
  [ "$FAILED" -eq 0 ] || exit 1
  exit 0
fi

# all steps
ensure_baseline
echo "Clearing any leftover simulated state"
purge
echo
i=1
while [ "$i" -le "$LAST" ]; do
  run_step "$i"
  echo
  sleep 2
  i=$((i + 1))
done

printf '%s\n' "============================================================"
echo "Expected 6 emails in total across the seven steps."
echo "Steps 3 and 7 proving SILENCE matters as much as the rest."
echo
echo "Clear the simulated state when you are done:"
echo "  ./test-alerts.sh -s $STACK -r $REGION --reset"
echo
if [ "$FAILED" -gt 0 ]; then
  echo "RESULT: $FAILED step(s) did not match expectations."
  exit 1
fi
echo "RESULT: all steps matched expectations."
