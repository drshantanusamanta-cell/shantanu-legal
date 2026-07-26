"""
Password gate for the dashboard.

Storage, in order of preference:

  APP_PASSWORD_SHA256   sha256 hex digest of the password        <- recommended
  APP_PASSWORD          plaintext password                        <- convenience

Prefer the hash. Streamlit Cloud secrets are not encrypted at rest in a way
that protects against someone with access to your dashboard, and you may reuse
this password elsewhere; storing only a digest limits the blast radius.

Generate a digest:
    python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"

Multi-user mode: set APP_USERS as a TOML table in secrets.toml:
    [APP_USERS]
    shantanu = "<sha256 hex>"
    associate = "<sha256 hex>"

Protections: constant-time comparison, attempt throttling with backoff, and an
idle-session timeout. This is deliberately modest — it gates a private tool, it
is not an identity provider. For a multi-tenant or client-data deployment, put
real SSO in front of it.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import streamlit as st

from config import COPYRIGHT_NOTICE, get_secret


MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 300          # 5 minutes after MAX_ATTEMPTS
SESSION_TIMEOUT_SECONDS = 8 * 3600


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _configured_users() -> dict[str, str]:
    """Return {username: sha256hex}. Empty dict means single-password mode."""
    users = get_secret("APP_USERS")
    if not users:
        return {}
    try:
        return {str(k): str(v).lower().strip() for k, v in dict(users).items()}
    except Exception:
        return {}


def _single_password_digest() -> str | None:
    digest = get_secret("APP_PASSWORD_SHA256")
    if digest:
        return str(digest).lower().strip()
    plain = get_secret("APP_PASSWORD")
    if plain:
        return sha256_hex(str(plain))
    return None


def auth_configured() -> bool:
    return bool(_configured_users()) or bool(_single_password_digest())


def _check(username: str, password: str) -> bool:
    supplied = sha256_hex(password)

    users = _configured_users()
    if users:
        expected = users.get(username.strip())
        if not expected:
            # Still burn a comparison so timing does not leak valid usernames.
            hmac.compare_digest(supplied, "0" * 64)
            return False
        return hmac.compare_digest(supplied, expected)

    expected = _single_password_digest()
    if not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def _init_state() -> None:
    st.session_state.setdefault("auth_ok", False)
    st.session_state.setdefault("auth_user", "")
    st.session_state.setdefault("auth_attempts", 0)
    st.session_state.setdefault("auth_locked_until", 0.0)
    st.session_state.setdefault("auth_login_time", 0.0)


def logout() -> None:
    for k in ("auth_ok", "auth_user", "auth_login_time"):
        st.session_state[k] = False if k == "auth_ok" else ("" if k == "auth_user" else 0.0)


def _session_expired() -> bool:
    t = st.session_state.get("auth_login_time", 0.0)
    return bool(t) and (time.time() - t > SESSION_TIMEOUT_SECONDS)


def require_login(app_title: str = "Legal Assistant") -> bool:
    """
    Gate the app. Returns True if the user may proceed.

    Call this as the FIRST thing in app.py, immediately after set_page_config,
    and `st.stop()` if it returns False.
    """
    _init_state()

    # No password configured at all -> refuse to run rather than run wide open.
    if not auth_configured():
        st.error("### 🔒 No access password configured")
        st.markdown(
            """
This app handles privileged and confidential material, so it will not start
without a password.

**Set one before running.** Generate a hash:

```bash
python -c "import hashlib,getpass;print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

Then add it to `.streamlit/secrets.toml` (local) or the **Secrets** box in
Streamlit Cloud → *Settings* → *Secrets*:

```toml
APP_PASSWORD_SHA256 = "paste_the_hex_digest_here"
```

For several users instead:

```toml
[APP_USERS]
shantanu  = "sha256_hex_of_their_password"
associate = "sha256_hex_of_their_password"
```
"""
        )
        return False

    if st.session_state.auth_ok and _session_expired():
        logout()
        st.warning("Session expired after 8 hours. Please sign in again.")

    if st.session_state.auth_ok:
        return True

    # ---------------- login form ----------------
    locked_until = st.session_state.get("auth_locked_until", 0.0)
    now = time.time()

    st.markdown(
        f"""
        <div style="text-align:center; padding:2.5rem 0 1rem 0;">
          <div style="font-size:3rem;">⚖️</div>
          <h1 style="margin:0.2rem 0; font-family:Georgia,serif;">{app_title}</h1>
          <p style="color:#64748b; margin-top:0.2rem;">Authorised access only</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        if now < locked_until:
            wait = int(locked_until - now)
            st.error(f"Too many failed attempts. Try again in {wait // 60}m {wait % 60}s.")
            return False

        multi = bool(_configured_users())
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input("Username", key="login_user") if multi else ""
            password = st.text_input("Password", type="password", key="login_pass")
            submitted = st.form_submit_button("Sign in", use_container_width=True, type="primary")

        if submitted:
            if _check(username, password):
                st.session_state.auth_ok = True
                st.session_state.auth_user = username or "user"
                st.session_state.auth_attempts = 0
                st.session_state.auth_login_time = time.time()
                st.rerun()
            else:
                st.session_state.auth_attempts += 1
                remaining = MAX_ATTEMPTS - st.session_state.auth_attempts
                if remaining <= 0:
                    st.session_state.auth_locked_until = time.time() + LOCKOUT_SECONDS
                    st.session_state.auth_attempts = 0
                    st.error("Too many failed attempts. Locked for 5 minutes.")
                else:
                    # Linear backoff blunts scripted guessing.
                    time.sleep(min(2.0, 0.4 * st.session_state.auth_attempts))
                    st.error(f"Incorrect credentials. {remaining} attempt(s) remaining.")

        st.caption(
            "This tool produces research assistance and drafts, not legal advice. "
            "Do not upload privileged client material to a deployment you do not control."
        )
        st.markdown(
            f"""
            <div style="text-align:center; margin-top:1.6rem; padding-top:1rem;
                        border-top:1px solid #e2e8f0; font-family:Georgia,serif;
                        font-size:0.82rem; color:#64748b;">
              {COPYRIGHT_NOTICE}
            </div>
            """,
            unsafe_allow_html=True,
        )

    return False


def current_user() -> str:
    return st.session_state.get("auth_user", "") or "user"


def render_logout_button() -> None:
    if st.sidebar.button("Sign out", use_container_width=True):
        logout()
        st.rerun()
