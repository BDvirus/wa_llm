# הקשחת אבטחה לפריסה ב-Komodo: Traefik + Authentik, אימות webhook, וסודות

> **סטטוס: מומש חלקית.** ממשק הניהול (`/admin`) הושלם, ו**סעיף 3 (גבול הרשת) מומש יחד איתו** —
> ראו ההערה בראש סעיף 3. שאר הסעיפים (1, 2, 4, 5) עדיין נדחים.
>
> **לבחון מחדש כשהממשק יהיה מוכן:**
> - ההחלטה להשאיר את Postgres חשוף ב-LAN נבעה רק מהצורך בניהול דרך SQL. עם UI אפשר
>   להסיר את פרסום הפורט `5432` לגמרי.
> - ממשק הניהול מוסיף פעולות כתיבה. אסור לחשוף אותו על פורט מפורסם בלי הגנה, ולכן הוא צריך
>   להיפרס מאחורי Traefik + Authentik (סעיף 3) או יחד עם התוכנית הזו.

## Context

הפרויקט ייפרס ב-Komodo ברשת המקומית, וממשקי הניהול יהיו זמינים רק דרך Traefik עם Authentik.
כרגע אין שום אימות, ו-Authentik לבדו **לא** יגן: הוא מגן רק על מה שעובר דרכו, ובמצב הנוכחי
כל שירות מפרסם פורט על `0.0.0.0` וניתן להגיע אליו ישירות מכל מכשיר ברשת.

מה אומת בקוד:
- **`POST /webhook` פתוח לגמרי**, ו-`sender_jid` נלקח מה-body הלא-מאומת (`models/message.py:108`).
  כל מי שמגיע לשירות יכול לזייף הודעה מכל JID ולעקוף את ההרשאה של `/kb_qa`
  (`handler/__init__.py:103`), להרעיל את בסיס הידע ואת הסיכומים, לשנות opt-out של אחרים,
  ולגרום לבוט לפרסם בקבוצות.
- **gowa מפורסם ב-`3003` עם `admin:admin`** — שליטה מלאה בחשבון הוואטסאפ.
- **הסיסמה `password` מקודדת בארבעה מקומות**: `POSTGRES_PASSWORD` ושלושה `DB_URI`.
  ה-DB של gowa (`webhook_db`) מחזיק גם את מפתחות ה-session של WhatsApp.

המטרה: שהגבול יהיה הרשת (Traefik + Authentik), עם הגנה בעומק במקומות שבהם הרשת לא מספיקה.

### עובדות שההחלטות נשענות עליהן

- **gowa תמיד חותם** (אומת בקוד המקור של v9.3.1): `X-Hub-Signature-256: sha256=<hex>`,
  HMAC-SHA256 בהקסה קטנה על בייטי ה-JSON המדויקים. המפתח מ-`WHATSAPP_WEBHOOK_SECRET`,
  וברירת המחדל היא `"secret"`.
- **gowa זורק אירועים:** timeout של 10 שניות; על כל תגובה שאינה 2xx — 5 ניסיונות
  (backoff 1/2/4/8 שניות), ואז **האירוע אובד לצמיתות**. לכן אי-התאמה בסוד = הודעות שנעלמות.
- **נבדק בניסוי:** dependency ברמת ה-route שקורא `await request.body()` רואה את הבייטים המדויקים,
  ו-`payload: WebhookEnvelope` עדיין מפוענח. ה-dependency רץ לפני ש-`get_handler` פותח session.
- **compose מצרף `ports` דרך `extends`, ולא מחליף אותם** — פורט שמפורסם ב-base אי אפשר להסיר ב-prod.
- **Komodo** כותב את ה-Environment של ה-stack ל-"Env File Path" ומעביר אותו ב-`--env-file`,
  כך ש-`${VAR}` עובד בקובץ ה-compose. אפשר להזריק Komodo Secrets עם `[[NAME]]`.
- **gowa UI משתמש ב-websockets** (`src/ui/websocket`). Traefik מעביר אותם כברירת מחדל.

## החלטות

| נושא | החלטה |
| --- | --- |
| גבול ראשי | Traefik + Authentik. **אין פורטים מפורסמים** ל-web-server ול-gowa ב-prod |
| Postgres | נשאר חשוף ב-LAN (`5432`), בסיסמה חזקה מ-Komodo secret |
| אימות webhook | HMAC חובה. הסיבה: שירותים ברשת ה-proxy המשותפת מגיעים ל-`web-server:8000` ישירות, בלי Authentik |
| API key | **לא בשלב הזה** — בני אדם עוברים דרך Authentik, ואין עדיין קוראים אוטומטיים. יתווסף עם התזמון |
| מקור אמת לסודות | משתני הסביבה של ה-stack ב-Komodo, מוזרקים לשני הקונטיינרים באותה אינטרפולציה |

## השינויים

### 1. סוד ה-webhook באפליקציה

**`src/config/__init__.py`** — שדה חובה, fail-fast בעלייה (כמו מפתחות ה-LLM):

```python
_MIN_WEBHOOK_SECRET_LENGTH: Final = 16
_GOWA_DEFAULT_WEBHOOK_SECRET: Final = "secret"  # gowa's public default

    whatsapp_webhook_secret: SecretStr

    @field_validator("whatsapp_webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if raw == _GOWA_DEFAULT_WEBHOOK_SECRET:
            raise ValueError("WHATSAPP_WEBHOOK_SECRET is gowa's public default 'secret'")
        if len(raw) < _MIN_WEBHOOK_SECRET_LENGTH:
            raise ValueError(f"WHATSAPP_WEBHOOK_SECRET must be at least {_MIN_WEBHOOK_SECRET_LENGTH} characters")
        return v
```

`SecretStr` מונע דליפה ב-`repr` ובלוגים. `apply_env` לא נוגע בשדה הזה.

**`src/api/security.py`** (חדש):

```python
SIGNATURE_HEADER: Final = "X-Hub-Signature-256"

def compute_signature(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

async def verify_webhook_signature(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> None:
    received = request.headers.get(SIGNATURE_HEADER)
    client = request.client.host if request.client else "unknown"
    if received is None:
        logger.error("Webhook rejected: missing %s (client=%s) - not sent by gowa?", SIGNATURE_HEADER, client)
        raise HTTPException(status_code=401, detail="Missing signature")
    expected = compute_signature(settings.whatsapp_webhook_secret.get_secret_value().encode(), await request.body())
    # Starlette decodes headers as latin-1, so re-encoding cannot fail; comparing str
    # with compare_digest raises TypeError on non-ASCII input and would become a 500.
    if not hmac.compare_digest(expected.encode(), received.encode("latin-1")):
        logger.error("Webhook rejected: signature mismatch (client=%s) - WHATSAPP_WEBHOOK_SECRET must match on gowa and web-server", client)
        raise HTTPException(status_code=401, detail="Invalid signature")
```

רמת `ERROR` בכוונה, עם הפרדה בין "חסר" (כנראה לא gowa) ל"לא תואם" (כנראה סוד שגוי).
**אף פעם** לא לרשום את ה-body או את החתימה.

**`src/api/webhook.py`** — `@router.post("/webhook", dependencies=[Depends(verify_webhook_signature)])`.
ברמת ה-route ולא כפרמטר, כדי שהטסטים הקיימים ב-`test_webhook.py`, שקוראים לפונקציה ישירות,
ימשיכו לעבוד בלי שינוי.

### 2. סודות ב-compose — מקור אמת אחד

כל סוד מוזרק ב-`${VAR:?}` **גם ל-gowa וגם ל-web-server**, כך ששניהם מקבלים את אותו ערך
מאותו מקור אינטרפולציה, בלי תלות ב-`env_file`. ב-web-server הערכים מוגדרים ב-`environment:`
במפורש (הוא גובר על `env_file`).

| משתנה | postgres | gowa | web-server |
| --- | --- | --- | --- |
| `POSTGRES_PASSWORD` | `POSTGRES_PASSWORD` | בתוך `DB_URI` | בתוך `DB_URI` |
| `WHATSAPP_WEBHOOK_SECRET` | — | `WHATSAPP_WEBHOOK_SECRET` | `WHATSAPP_WEBHOOK_SECRET` |
| `WHATSAPP_BASIC_AUTH_USER` / `_PASSWORD` | — | `APP_BASIC_AUTH=${USER}:${PASSWORD}` | `WHATSAPP_BASIC_AUTH_*` |

זה מחליף את ארבעת המופעים של `password` ואת `admin:admin`.

### 3. גבול הרשת

> **✓ מומש** יחד עם ממשק הניהול, עם שני תיקונים שחסרו בתוכנית המקורית:
> - `networks: [default, proxy]` **במפורש** — בלי `default` ברשימה, השירותים מאבדים את postgres ואת gowa.
> - `FORWARDED_ALLOW_IPS=*` ל-web-server — אחרת uvicorn לא סומך על `X-Forwarded-Proto` מ-Traefik
>   ובונה קישורי `http://` שהדפדפן חוסם בדף https.
>
> - **aliases ייחודיים לכל שירות** (`wa-llm-postgres`, `wa-llm-gowa`, `wa-llm-web`) ברשת הפנימית,
>   ושימוש בהם בכל החיבורים הפנימיים. רשת ה-proxy משותפת לכל ה-stacks, ושם גנרי כמו `postgres`
>   עלול להיפתר ל-Postgres של stack אחר — זה בדיוק מה שהפיל את gowa בפריסה הראשונה
>   (`password authentication failed for user "user"`).
>
> כמו כן, `ports` של gowa עבר מ-`base` לקבצי הפיתוח. Postgres נשאר על `5432` לפי החלטה.
> אגב כך תוקן באג קיים: `docker-compose.yml` ו-`docker-compose.local-run.yml` לא הצהירו על
> ה-volume `wa_llm_whatsapp_statics`, ולכן היו לא תקינים.

- **`docker-compose.base.yml`** — להסיר את `ports` משירות `whatsapp` (בגלל שה-`ports` מצטרפים
  דרך `extends`). ה-`5432` של postgres נשאר, לפי ההחלטה.
- **`docker-compose.yml`, `docker-compose.local-run.yml`** — להחזיר `ports: ["3003:3000"]`
  ל-`whatsapp`, כך שסביבת הפיתוח לא משתנה.
- **`docker-compose.prod.yml`** (הקובץ שנפרס ב-Komodo):
  - להסיר את `ports: 8000:8000` מ-web-server.
  - web-server ו-whatsapp מצטרפים לרשת `default` ולרשת ה-proxy החיצונית. postgres נשאר רק ב-`default`.
  - labels של Traefik לשני השירותים, מפורמטרים כך שאין בריפו שום דבר ספציפי לרשת שלך:

```yaml
    labels:
      - traefik.enable=true
      - traefik.docker.network=${TRAEFIK_NETWORK:-proxy}
      - traefik.http.routers.wa-llm.rule=Host(`${WA_LLM_HOST:?set WA_LLM_HOST}`)
      - traefik.http.routers.wa-llm.entrypoints=${TRAEFIK_ENTRYPOINT:-websecure}
      - traefik.http.routers.wa-llm.tls=true
      - traefik.http.routers.wa-llm.middlewares=${AUTHENTIK_MIDDLEWARE:-authentik@docker}
      - traefik.http.services.wa-llm.loadbalancer.server.port=8000
networks:
  proxy:
    external: true
    name: ${TRAEFIK_NETWORK:-proxy}
```

  ל-gowa אותו דבר עם router בשם `gowa`, `${GOWA_HOST}` ופורט `3000`.

  **הנחות:** רשת Traefik חיצונית קיימת, middleware של forward-auth ל-Authentik כבר מוגדר
  (בדרך כלל כ-labels על ה-outpost), ו-entrypoint עם TLS ו-certresolver ברירת מחדל.
  כל אחד מאלה ניתן לשינוי דרך משתנה.

### 4. `.dockerignore`

להוסיף `.env.*` ו-`!.env.example`. היום רק `.env` מוחרג, ו-`COPY . /app` אופה לתוך ה-image
כל `.env.prod` שקיים בתקיית הבנייה — כולל קובץ ש-Komodo כותב לשם.

### 5. תיעוד

- **`.env.example`** — המשתנים החדשים, עם הוראה לייצר ב-`openssl rand -hex 32`.
- **`README.md`** — פרק פריסה ב-Komodo: Komodo Secrets, משתני Traefik, סדר הפריסה (בהמשך),
  ואזהרה ש-`${VAR:?}` גורם גם ל-`docker compose ps/logs/down` להיכשל כשמשתנה חסר.
- **`AGENTS.md`** — `WHATSAPP_WEBHOOK_SECRET` ברשימת החובה. גם ה-notebook וה-alembic המקומי יצטרכו אותו.
- **`docs/02-message-flow.mmd`** — צומת אימות חתימה אחרי `POST /webhook`, עם יציאה ל-401.
  **`docs/01-architecture-boundary.mmd`** — הערה על הסוד המשותף ועל הרשתות.

## פריסה ב-Komodo — הסדר חשוב

1. **לייצר סודות בהקסה:** `openssl rand -hex 32` לכל אחד מ-`WHATSAPP_WEBHOOK_SECRET`,
   `POSTGRES_PASSWORD`, `WHATSAPP_BASIC_AUTH_PASSWORD`. הקסה ולא תווים חופשיים, כי `$` נקרא
   כמשתנה בקבצי env, `:` ו-`,` שוברים את `APP_BASIC_AUTH`, ו-`@` או `/` שוברים את `DB_URI`.
   לשמור כ-Komodo Secrets ולהפנות אליהם מה-Environment של ה-stack עם `[[...]]`.
2. **אם ה-volume של postgres כבר קיים** — `POSTGRES_PASSWORD` חל **רק באתחול הראשון**. לפני
   הפריסה, בתוך הקונטיינר הרץ:
   `ALTER USER "user" WITH PASSWORD '<new>';`
   (`user` היא מילה שמורה, ולכן חובה במירכאות.) בלי זה שני השירותים יאבדו גישה ל-DB.
3. **לפרוס את ה-stack.** gowa עולה לפני web-server (`depends_on`), וה-retries שלו (~15 שניות)
   מכסים את חלון ההחלפה.
4. **לבדוק מיד בלוגים שאין `Webhook rejected`.** אם יש — הסוד לא תואם, והודעות **אובדות** כרגע.

## מחוץ לתחום (במודע)

- **API key** — יתווסף יחד עם התזמון. סיכון שנותר: קונטיינר אחר ברשת ה-proxy יכול לקרוא
  ישירות ל-`/load_new_kbtopics` ול-`/summarize_and_send_to_groups`.
- **הגנה מפני replay** — ל-gowa אין timestamp בחתימה, ו-replay דורש גישה לרשת ה-docker. מתועד ומקובל.
- **תקלות קיימות ב-local-run** — `PORT` ברירת מחדל 5001 מול 8000, `WHATSAPP_HOST` שמצביע על 3000
  במקום 3003, ו-`host.docker.internal` שדורש `extra_hosts` בלינוקס.
- **עבודת LLM בתוך בקשת ה-webhook** מול ה-timeout של gowa (10 שניות) — קיים כבר היום וגורם
  לכפילויות. שייך לתיקון נפרד, וחשוב שלא ייוחס לשינוי הזה.

## אימות

**טסטים** (`src/api/test_security.py` חדש, והרחבה של `src/config/test_config.py`):
- `compute_signature` מול וקטור ידוע: HMAC-SHA256 עם key=`"key"` על
  `"The quick brown fox jumps over the lazy dog"` =
  `f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8`.
- אפליקציית FastAPI זעירה עם `dependency_overrides[get_settings]` (בלי ה-lifespan האמיתי):
  חתימה תקינה → 200 **וה-payload מפוענח**; לא תואמת → 401; חסרה → 401;
  **header לא-ASCII → 401 ולא 500** (רגרסיה למלכודת ה-compare_digest); body ששונה אחרי החתימה → 401.
- `Settings`: סוד חסר, `"secret"` או קצר מ-16 → `ValidationError`; ו-`repr(settings)` לא מכיל את הסוד.
- הוספת הסוד ל-`_BASE` בטסטי ה-config. `test_webhook.py` נשאר בלי שינוי.
- `uv run poe check` ירוק.

**compose** (בלי daemon): `docker compose --env-file <test.env> -f docker-compose.prod.yml config`
— לוודא שלא נשאר `ports` ל-web-server ול-whatsapp, שה-labels וה-`DB_URI` נפתרים, ושמשתנה חסר נכשל
עם הודעת ה-`:?`. אם ה-CLI של docker לא זמין במחשב, זה ייבדק ב-Komodo.

**ב-Komodo אחרי הפריסה:**
1. מכשיר אחר ב-LAN: `curl http://<komodo-host>:8000` ו-`:3003` → connection refused.
2. `https://${WA_LLM_HOST}/docs` ו-`https://${GOWA_HOST}` → הפניה ל-Authentik, ואחריה עובד
   (דף ה-QR של gowa נטען, כלומר ה-websocket עובר).
3. הודעה עם תיוג בקבוצה מנוהלת → תשובה, ואין `Webhook rejected` בלוגים.
4. זיוף מתוך רשת ה-proxy מחזיר 401:
   `docker run --rm --network <proxy> curlimages/curl -s -o /dev/null -w "%{http_code}" -X POST http://web-server:8000/webhook -H "Content-Type: application/json" -d '{"event":"message","payload":{}}'`
