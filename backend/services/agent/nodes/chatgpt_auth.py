"""IGNORE THIS FILE

Local, single-account ChatGPT OAuth. Credentials stay outside graph state.

Official flow: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
"""

import base64
import hashlib
import json
import os
import secrets
import ssl
import tempfile
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import certifi

AUTH_URL = "https://auth.openai.com/api/accounts/authorize"
TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
RESOURCE = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
# Local credentials are ignored by Git and written with owner-only permissions.
AUTH_DIR = Path(__file__).resolve().parents[1] / ".chatgpt-auth"


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    AUTH_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(AUTH_DIR, 0o700)
    descriptor, temporary = tempfile.mkstemp(dir=AUTH_DIR)
    try:
        with os.fdopen(descriptor, "w") as file:
            json.dump(data, file)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _exchange(data: dict) -> dict:
    response = httpx.post(TOKEN_URL, data=data, timeout=30)
    if response.is_error:
        # Do not print responses that could contain credentials.
        raise RuntimeError("ChatGPT token exchange failed. Run sign-in again.")
    return response.json()


def _credential_record(tokens: dict, previous: dict) -> dict:
    scopes = tokens.get("scope", " ".join(previous.get("scopes", []))).split()
    if "chatgpt.tokens.use.direct" not in scopes:
        raise RuntimeError("ChatGPT plan usage was not authorized.")
    if not tokens.get("access_token"):
        raise RuntimeError("Sign-in did not provide an access token.")
    return {
        **previous,
        **tokens,
        "scopes": scopes,
        "expires_at": time.time() + float(tokens["expires_in"]),
    }


def sign_in() -> str:
    """Open browser sign-in, validate identity, and save this account's tokens."""
    host_path = AUTH_DIR / "host.json"
    host = _read(host_path)
    if not host:
        host = {"ext_agent_host_id": f"urn:uuid:{uuid.uuid4()}"}
        _save(host_path, host)
    registration_path = AUTH_DIR / "registration.json"
    registration = _read(registration_path)
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    callback = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/auth/callback":
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            received_state = query.get("state", [""])[0]
            if not secrets.compare_digest(received_state, state):
                self.send_error(400, "Invalid OAuth state")
                return
            callback.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Callback received. Return to your terminal to finish sign-in.")

    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/auth/callback"
        params = {
            "client_id": registration.get("client_id", "dynamic_agent_client"),
            "ext_agent_host_id": host["ext_agent_host_id"],
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct",
            "resource": RESOURCE,
            "state": state,
            "nonce": nonce,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
        if not registration:
            params["agent_name_hint"] = "TravelAgentic"
        print("Continue with ChatGPT in your browser.")
        url = f"{AUTH_URL}?{urlencode(params)}"
        if not webbrowser.open(url):
            print(f"Open this sign-in link: {url}")
        deadline = time.monotonic() + 300
        server.timeout = 1
        while not callback and time.monotonic() < deadline:
            server.handle_request()

    if not callback:
        raise RuntimeError("ChatGPT sign-in timed out. Try again.")
    if callback.get("error"):
        raise RuntimeError("ChatGPT sign-in was declined or failed.")
    client_id = callback.get("client_id", registration.get("client_id"))
    if not client_id or client_id == "dynamic_agent_client" or not callback.get("code"):
        raise RuntimeError("ChatGPT registration did not complete.")
    if registration and client_id != registration["client_id"]:
        raise RuntimeError("Callback client ID does not match this registration.")
    # Retain registration even if exchange fails, as the code cannot be reused.
    _save(registration_path, {**registration, "client_id": client_id})
    tokens = _exchange({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "code": callback["code"],
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
        "resource": RESOURCE,
    })
    discovery = httpx.get(f"{ISSUER}/.well-known/openid-configuration", timeout=30)
    discovery.raise_for_status()
    metadata = discovery.json()
    if metadata["issuer"] != ISSUER:
        raise RuntimeError("Unexpected identity issuer.")
    id_token = tokens["id_token"]
    # Python.org macOS installs may lack a configured system CA bundle.
    # Use certifi, as HTTPX does, without disabling TLS verification.
    tls_context = ssl.create_default_context(cafile=certifi.where())
    key = jwt.PyJWKClient(
        metadata["jwks_uri"], ssl_context=tls_context
    ).get_signing_key_from_jwt(id_token)
    identity = jwt.decode(
        id_token, key.key, algorithms=["RS256"], audience=client_id, issuer=ISSUER,
        options={"require": ["exp", "iss", "aud", "sub", "nonce"]},
    )
    if not secrets.compare_digest(identity["nonce"], nonce):
        raise RuntimeError("ID token nonce does not match sign-in.")
    if registration.get("subject") and identity["sub"] != registration["subject"]:
        raise RuntimeError("Sign-in returned a different account.")
    account = {"client_id": client_id, "subject": identity["sub"], **host}
    credentials = _credential_record(tokens, account)
    _save(registration_path, account)
    _save(AUTH_DIR / "credentials.json", credentials)
    print("ChatGPT connected.")
    return credentials["access_token"]


def get_access_token() -> str:
    """Reuse or refresh saved credentials; sign in when none exist.

    This prototype runs in one local process. Concurrent hosted sessions need
    per-account storage and serialized refreshes.
    """
    path = AUTH_DIR / "credentials.json"
    credentials = _read(path)
    if not credentials:
        return sign_in()
    if "chatgpt.tokens.use.direct" not in credentials.get("scopes", []):
        raise RuntimeError("ChatGPT plan usage is not enabled. Run sign-in again.")
    if time.time() >= credentials["expires_at"] - 60:
        if not credentials.get("refresh_token"):
            return sign_in()
        tokens = _exchange({
            "grant_type": "refresh_token",
            "client_id": credentials["client_id"],
            "refresh_token": credentials["refresh_token"],
            "resource": RESOURCE,
        })
        credentials = _credential_record(tokens, credentials)
        _save(path, credentials)
    return credentials["access_token"]


def choose_model() -> str:
    response = httpx.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {get_access_token()}"},
        timeout=30,
    )
    response.raise_for_status()
    models = [m for m in response.json()["models"] if m.get("visibility") == "list"]
    if not models:
        raise RuntimeError("No models are available for this ChatGPT account.")
    for number, model in enumerate(models, 1):
        print(f"{number}. {model['display_name']} ({model['slug']})")
    while True:
        try:
            choice = int(input("Choose model number: "))
            if 1 <= choice <= len(models):
                return models[choice - 1]["slug"]
        except ValueError:
            pass
        print("Enter one of the listed numbers.")


if __name__ == "__main__":
    sign_in()
