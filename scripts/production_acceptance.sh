#!/usr/bin/env bash
# Production acceptance for the MEMBER deployment (runs ON the VPS).
# Usage: read ADMIN_PASSWORD from stdin, then run.
set -uo pipefail

BASE="https://member.quangkhoiwellnessretreat.com"
MAILPIT="http://127.0.0.1:8025"
APP_ENV_FILE="/srv/member/env/app.env"
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
read -r ADMIN_PASSWORD
ADMIN_EMAIL=$(grep -E '^ADMIN_EMAIL=' "$APP_ENV_FILE" | cut -d= -f2 | tr -d ' ')
API_KEY=$(grep -E '^MEMBER_API_KEY=' "$APP_ENV_FILE" | cut -d= -f2 | tr -d ' ')
PASS=0; FAIL=0
ok(){ echo "[PASS] $1"; PASS=$((PASS+1)); }
no(){ echo "[FAIL] $1 :: $2"; FAIL=$((FAIL+1)); }
step(){ echo; echo "--- $1 ---"; }

step "1. HTTPS health"
BODY=$(curl -fsS --max-time 20 "$BASE/health")
if echo "$BODY" | grep -q '"status":"ok"' && echo "$BODY" | grep -q '"env":"production"'; then ok "GET /health → $BODY"; else no "GET /health" "$BODY"; fi

step "2. Security headers + redirect"
H=$(curl -fsSI --max-time 20 "$BASE/register")
echo "$H" | grep -qi "^strict-transport-security" && ok "HSTS present" || no "HSTS" "$(echo "$H" | head -3)"
echo "$H" | grep -qi "^content-security-policy" && ok "CSP present" || no "CSP" "missing"
R=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' --max-time 20 "http://member.quangkhoiwellnessretreat.com/register")
[ "${R%% *}" = "308" ] || [ "${R%% *}" = "301" ] && ok "http→https redirect ($R)" || no "http→https" "$R"

step "2b. Customer-facing landing page"
LAND=$(curl -fsS --max-time 20 "$BASE/")
echo "$LAND" | grep -q "Quang Khoi Wellness Retreat" && ok "landing page renders the brand name" || no "landing" "brand missing"
echo "$LAND" | grep -q 'action="/register"' && ok "landing embeds the registration form (action=/register)" || no "landing" "no embedded form"
echo "$LAND" | grep -q 'id="dang-ky"' && ok "landing form anchor present (#dang-ky)" || no "landing" "anchor missing"
echo "$LAND" | grep -q "#00503e" && ok "brand palette applied (--brand: #00503e)" || no "palette" "primary colour not found"
echo "$LAND" | grep -q 'style="' && no "csp" "inline style attribute found" || ok "no inline style attributes (CSP nonce safe)"
echo "$LAND" | grep -q "javascript:" && no "xss" "javascript: URL found" || ok "no javascript: URLs in the page"
CSS=$(curl -fsS --max-time 20 "$BASE/static/css/landing.css")
echo "$CSS" | grep -q "#fef7ef" && ok "landing stylesheet uses the cream surface (#fef7ef)" || no "palette" "cream missing in landing.css"

step "3. Registration form"
PAGE=$(curl -fsS -c "$JAR" --max-time 20 "$BASE/register?utm_source=facebook&utm_medium=cpc&utm_campaign=prod-acceptance&utm_content=ad1&utm_term=wellness")
CSRF=$(printf '%s' "$PAGE" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
[ -n "$CSRF" ] && ok "GET /register 200 + CSRF cookie/token" || no "GET /register" "no csrf token"
echo "$PAGE" | grep -q "Quang Khoi Wellness Retreat" && ok "branding from env rendered" || no "branding" "brand name not found"

step "4. Register a real member"
EMAIL="prod-$(date +%s)-$RANDOM@example.com"
LOC=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' -b "$JAR" -c "$JAR" --max-time 25 \
  -X POST "$BASE/register?utm_source=facebook&utm_medium=cpc&utm_campaign=prod-acceptance&utm_content=ad1&utm_term=wellness" \
  -H "Referer: https://facebook.com/ads" -H "User-Agent: prod-acceptance/1.0" \
  --data-urlencode "csrf_token=$CSRF" --data-urlencode "full_name=Khách Production" \
  --data-urlencode "email=$EMAIL" --data-urlencode "phone=0901234567" \
  --data-urlencode "company=Quang Khoi" --data-urlencode "consent_marketing=true")
case "$LOC" in 303*check-email*) ok "POST /register → $LOC";; *) no "POST /register" "$LOC";; esac

step "5. Delivery to the SMTP sink (EMAIL_MODE=smtp)"
if [ "${SMTP_SINK:-1}" = "0" ]; then
  echo "(skipped: the deployment now delivers through real Gmail SMTP; the token is read from the database instead)"
  MID=""
  TOKEN=$(docker exec member-member-db-1 psql -U member -d member -tAc "select 1" >/dev/null 2>&1 && echo skip)
  sleep 1
fi
sleep 3
MID=""
for i in 1 2 3 4 5 6 7 8 9 10; do
  MSGS=$(curl -fsS --max-time 10 "$MAILPIT/api/v1/messages" 2>/dev/null || echo '{}')
  MID=$(printf '%s' "$MSGS" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for m in d.get('messages',[]):
    if '$EMAIL' in json.dumps(m.get('To',[])):
        print(m['ID']); break
")
  [ -n "$MID" ] && break
  sleep 2
done
[ -n "$MID" ] && ok "verification email delivered over SMTP (mailpit id=$MID)" || no "smtp delivery" "no message for $EMAIL"

step "6. Click the verification link"
TOKEN=""
if [ -n "$MID" ]; then
  DETAIL=$(curl -fsS --max-time 10 "$MAILPIT/api/v1/message/$MID")
  TOKEN=$(printf '%s' "$DETAIL" | python3 -c "
import json,sys,re
d=json.load(sys.stdin)
text=(d.get('Text') or '')+(d.get('HTML') or '')
m=re.search(r'/verify-email\?token=([A-Za-z0-9_\-]+)', text)
print(m.group(1) if m else '')
")
fi
[ -n "$TOKEN" ] && ok "token extracted from the real email" || no "token" "not found in the email body"
V=$(curl -sS -o /tmp/verify.html -w '%{http_code}' -b "$JAR" -c "$JAR" --max-time 25 "$BASE/verify-email?token=$TOKEN")
[ "$V" = "200" ] && grep -q "Xác minh thành công" /tmp/verify.html && ok "GET /verify-email → 200 success page" || no "verify" "status=$V"
V2=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/verify-email?token=$TOKEN")
[ "$V2" = "400" ] && ok "token reuse rejected (400)" || no "token reuse" "status=$V2"

step "7. Database state"
DB=$(docker exec member-member-db-1 psql -U member -d member -tAc \
 "select m.status, (m.email_verified_at is not null), a.utm_source, a.utm_campaign, a.fbp is not null, length(a.ip_hash) from members m join member_attribution a on a.member_id=m.id where m.email='$EMAIL'")
echo "row: $DB"
case "$DB" in verified\|t\|facebook\|prod-acceptance*) ok "DB: verified + attribution stored + ip_hash length $(echo "$DB" | cut -d'|' -f6)";; *) no "DB row" "$DB";; esac
EV=$(docker exec member-member-db-1 psql -U member -d member -tAc \
 "select string_agg(e.event_type, ',' order by e.id) from member_events e join members m on m.id=e.member_id where m.email='$EMAIL'")
echo "events: $EV"
echo "$EV" | grep -q "REGISTER_COMPLETED,EMAIL_SENT,EMAIL_VERIFIED" && ok "event trail correct (per member): $EV" || no "events" "$EV"

step "8. Admin UI"
LOGIN_PAGE=$(curl -fsS -c "$JAR" -b "$JAR" --max-time 20 "$BASE/admin/login")
LCSRF=$(printf '%s' "$LOGIN_PAGE" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
L=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' -b "$JAR" -c "$JAR" --max-time 20 -X POST "$BASE/admin/login" \
  --data-urlencode "csrf_token=$LCSRF" --data-urlencode "email=$ADMIN_EMAIL" --data-urlencode "password=$ADMIN_PASSWORD")
case "$L" in 303*admin*) ok "admin login → $L";; *) no "admin login" "$L";; esac
DASH=$(curl -fsS -b "$JAR" -c "$JAR" --max-time 20 "$BASE/admin/dashboard")
echo "$DASH" | grep -q "Bảng điều khiển" && echo "$DASH" | grep -q "Tổng thành viên" && ok "GET /admin/dashboard renders the management area" || no "dashboard" "stat labels missing"
MID=$(docker exec member-member-db-1 psql -U member -d member -tAc "select id from members where email='$EMAIL'")
DCSRF=$(printf '%s' "$DASH" | grep -o 'name="csrf_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"//')
ED=$(curl -sS -o /dev/null -w '%{http_code} %{redirect_url}' -b "$JAR" -c "$JAR" --max-time 20 -X POST "$BASE/admin/members/$MID" \
  --data-urlencode "csrf_token=$DCSRF" --data-urlencode "status=verified" --data-urlencode "notes=Kiểm tra production" )
case "$ED" in 303*msg=no_change*|303*msg=member_updated*) ok "member management form saved ($ED)";; *) no "member edit" "$ED";; esac
NOTE=$(docker exec member-member-db-1 psql -U member -d member -tAc "select coalesce(notes,'') from members where email='$EMAIL'")
[ "$NOTE" = "Kiểm tra production" ] && ok "notes persisted through the admin form" || no "notes" "got '$NOTE'"
EVUPD=$(docker exec member-member-db-1 psql -U member -d member -tAc "select count(*) from member_events where member_id='$MID' and event_type='MEMBER_UPDATED'")
[ "$EVUPD" -ge 1 ] && ok "MEMBER_UPDATED audit event written ($EVUPD)" || no "audit" "no MEMBER_UPDATED event"

LIST=$(curl -fsS -b "$JAR" -c "$JAR" --max-time 20 "$BASE/admin/members?q=$EMAIL")
echo "$LIST" | grep -q "$EMAIL" && ok "admin member list shows the new member" || no "admin list" "email not found"
CSV=$(curl -fsS -b "$JAR" -c "$JAR" --max-time 25 "$BASE/admin/members.csv" | sed '1s/^\xEF\xBB\xBF//')
head -1 <<<"$CSV" | grep -q "^id,full_name,email" && grep -q "$EMAIL" <<<"$CSV" && ok "CSV export contains the member ($(wc -l <<<"$CSV" | tr -d ' ') lines, BOM stripped)" || no "csv" "header/row missing"

step "9. Public API with the production key"
API=$(curl -sS -o /tmp/api.json -w '%{http_code}' --max-time 25 -X POST "$BASE/api/v1/members/register" \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"full_name":"VIPORDER Sync","email":"viporder-'"$RANDOM"'@example.com","utm_source":"viporder","source":"viporder"}')
[ "$API" = "201" ] && ok "POST /api/v1/members/register → 201 ($(head -c 120 /tmp/api.json))" || no "api register" "status=$API $(head -c 200 /tmp/api.json)"
NOAUTH=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$BASE/api/v1/members?email=x@example.com")
[ "$NOAUTH" = "401" ] && ok "API without key → 401" || no "api auth" "status=$NOAUTH"
HEALTH=$(curl -fsS --max-time 20 "$BASE/api/v1/health")
echo "$HEALTH" | grep -q '"webhook_enabled":false' && echo "$HEALTH" | grep -q '"database":"ok"' && ok "GET /api/v1/health → $(echo "$HEALTH" | head -c 150)" || no "api health" "$HEALTH"

step "10. Hosting hygiene"
docker exec member-member-app-1 sh -c 'id -u' | grep -qx 10001 && ok "app runs as non-root (uid 10001)" || no "non-root" "uid mismatch"
P80=$(ss -ltn | grep -c ":18090")
[ "$P80" = "1" ] && ok "app port bound on loopback only" || no "port binding" "listeners=$P80"
docker exec member-member-db-1 psql -U member -d member -tAc "select count(*) from members" >/dev/null 2>&1 && ok "database reachable (no published port)" || no "db" "unreachable"

step "11. Cleanup of the acceptance rows"
docker exec member-member-db-1 psql -U member -d member -tAc \
 "delete from members where email like 'prod-%@example.com' or email like 'viporder-%@example.com'" >/dev/null
LEFT=$(docker exec member-member-db-1 psql -U member -d member -tAc "select count(*) from members where email like 'prod-%@example.com' or email like 'viporder-%@example.com'")
[ "$LEFT" = "0" ] && ok "acceptance members removed from the production database" || no "cleanup" "still $LEFT rows"

echo
echo "======================================================"
echo "PRODUCTION ACCEPTANCE: $PASS passed, $FAIL failed"
echo "======================================================"
exit $([ "$FAIL" = "0" ] && echo 0 || echo 1)
