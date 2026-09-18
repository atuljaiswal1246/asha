# Security policy

## Reporting a vulnerability

Please report security issues **privately**. Do not open a public issue, and do
not post exploit details in a discussion or pull request.

Email **atul.j@hummingseo.com** with:

- a description of the issue and its impact,
- the steps or a minimal reproduction to trigger it,
- the version/commit you tested, and
- any suggested fix, if you have one.

You will get an acknowledgement, and we will keep you updated as we investigate
and work on a fix. Please give us a reasonable window to release a fix before
disclosing the issue publicly.

## Secrets never live in the repository

- API keys and provider tokens are provided by the user at runtime and written
  to a local `.env` file, which is **gitignored** (`.env`, `*.env`, `.env.*`,
  with `.env.example` as the only tracked template).
- In packaged builds the key is stored in the app's own resources on the user's
  machine, not in this repository.
- For the bring-your-own-key path there is no Asha account and no Asha
  server in the request path.

If you ever find a key, token, or other secret committed to this repository,
treat it as a security report: email the address above instead of opening a
public issue. Do not paste the secret itself into an issue or PR.
