# auth/login

`POST {email}` — find-or-create the user + mint an APIToken (scopes bot,tx,browser) via admin-api,
set the httpOnly `vexa-token` cookie. No email is sent.

DEV / E2E only: 404 unless `VEXA_TERMINAL_DEV_LOGIN=1` and `NODE_ENV != production`; 403 unless the
email exactly matches an entry of `VEXA_TERMINAL_DEV_LOGIN_EMAILS`. Never claims the first-run admin
role (see `devLogin.ts`).
