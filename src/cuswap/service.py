from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from cuswap import apply_switch, browser, cursor_db, cursor_proc, jwtutil, oauth, store, usage
from cuswap.models import Account, AccountIndex, AccountRow, EncodedKey, SessionBundle, UsageSnapshot, isoformat, utcnow
from cuswap.sealed import is_sealed
from cuswap.store import StoreError


class ServiceError(RuntimeError):
    pass


def read_live() -> SessionBundle:
    try:
        return cursor_db.read_live_bundle()
    except cursor_db.CursorDbError as exc:
        raise ServiceError(str(exc)) from exc


def identity_from_bundle(bundle: SessionBundle) -> dict:
    token = bundle.get("cursorAuth/accessToken")
    email = bundle.get("cursorAuth/cachedEmail")
    name = ""
    user_id = None
    auth_sub = None
    membership = bundle.get("cursorAuth/stripeMembershipType")
    if token:
        try:
            auth_sub = jwtutil.subject(token)
        except jwtutil.JwtError:
            auth_sub = None
        try:
            me = usage.fetch_me(token)
            email = me.get("email") or email
            name = me.get("name") or ""
            user_id = me.get("id")
        except (usage.UsageError, jwtutil.JwtError):
            pass
    if not email:
        raise ServiceError("Could not determine the signed-in email. Sign in to Cursor first.")
    return {
        "email": str(email),
        "name": str(name or ""),
        "user_id": int(user_id) if user_id is not None else None,
        "auth_sub": auth_sub,
        "membership_type": membership,
        "token": token,
    }


def bundle_from_tokens(access_token: str, refresh_token: str = "") -> SessionBundle:
    keys = [
        EncodedKey(key="cursorAuth/accessToken", value=access_token),
        EncodedKey(key="cursorAuth/cachedSignUpType", value="Auth_0"),
    ]
    if refresh_token:
        keys.append(EncodedKey(key="cursorAuth/refreshToken", value=refresh_token))
    bundle = SessionBundle(keys=keys)
    try:
        me = usage.fetch_me(access_token)
    except usage.UsageError:
        me = {}
    email = me.get("email")
    if email:
        bundle.set("cursorAuth/cachedEmail", str(email))
    try:
        summary = usage.fetch_usage_summary(access_token)
        membership = summary.get("membershipType")
        if membership:
            bundle.set("cursorAuth/stripeMembershipType", str(membership))
    except usage.UsageError:
        pass
    return bundle


def save_bundle_as_account(
    bundle: SessionBundle,
    *,
    slot: int | None = None,
    mark_live: bool = False,
) -> Account:
    info = identity_from_bundle(bundle)
    index = store.load_index()
    account = store.upsert_account(
        index,
        email=info["email"],
        name=info["name"],
        user_id=info["user_id"],
        auth_sub=info["auth_sub"],
        membership_type=info["membership_type"],
        slot=slot,
        bundle=bundle,
    )
    if mark_live:
        index = store.load_index()
        live_id = match_live_account(index, bundle)
        if live_id:
            index.active_id = live_id
            store.save_index(index)
    return account


def add_browser_account(
    *,
    slot: int | None = None,
    apply: bool = False,
    timeout: float = 240.0,
    on_start=None,
    on_wait=None,
) -> tuple[Account, str, str | None]:
    """Login via private browser window. Does not sign out the IDE or website."""
    session = oauth.start_session()
    opened = browser.open_private(session.login_url)
    if on_start:
        on_start(session.login_url, opened)
    try:
        tokens = oauth.poll_tokens(session, timeout=timeout, on_wait=on_wait)
    except oauth.OAuthError as exc:
        raise ServiceError(str(exc)) from exc
    bundle = bundle_from_tokens(tokens.access_token, tokens.refresh_token)
    account = save_bundle_as_account(bundle, slot=slot, mark_live=False)
    if apply:
        switch_to(str(account.slot), force=True)
    return account, session.login_url, opened


def add_current(*, slot: int | None = None) -> Account:
    bundle = read_live()
    return save_bundle_as_account(bundle, slot=slot, mark_live=True)


def match_live_account(index: AccountIndex, bundle: SessionBundle | None = None) -> str | None:
    try:
        live = bundle or read_live()
    except ServiceError:
        return None
    email = live.get("cursorAuth/cachedEmail")
    if email:
        found = index.by_email(email)
        if found:
            return found.id
    token = live.get("cursorAuth/accessToken")
    if not token:
        return None
    try:
        sub = jwtutil.subject(token)
    except jwtutil.JwtError:
        return None
    for account in index.accounts:
        if account.auth_sub == sub:
            return account.id
    return None


def _restore_plaintext_tokens(live: SessionBundle, stored: SessionBundle | None) -> None:
    if stored is None:
        return
    for key in ("cursorAuth/accessToken", "cursorAuth/refreshToken"):
        if is_sealed(live.get(key)):
            previous = stored.get(key)
            if previous and not is_sealed(previous):
                live.set(key, previous)


def _has_sealed_token(bundle: SessionBundle) -> bool:
    return any(
        is_sealed(bundle.get(key))
        for key in ("cursorAuth/accessToken", "cursorAuth/refreshToken")
    )


def sync_live_into_active() -> Account | None:
    index = store.load_index()
    try:
        bundle = read_live()
    except ServiceError:
        return None
    live_id = match_live_account(index, bundle)
    if not live_id:
        return None
    account = index.by_id(live_id)
    if not account:
        return None
    try:
        stored = store.load_bundle(account.id)
    except StoreError:
        stored = None
    _restore_plaintext_tokens(bundle, stored)
    if _has_sealed_token(bundle):
        return account
    store.save_bundle(account.id, bundle)
    account.updated_at = isoformat(utcnow()) or account.updated_at
    info_email = bundle.get("cursorAuth/cachedEmail")
    if info_email:
        account.email = info_email
    membership = bundle.get("cursorAuth/stripeMembershipType")
    if membership:
        account.membership_type = membership
    index.active_id = account.id
    store.save_index(index)
    return account


def token_for(account: Account) -> str | None:
    try:
        bundle = store.load_bundle(account.id)
    except StoreError:
        return None
    return bundle.get("cursorAuth/accessToken")


def token_status(account: Account) -> str:
    token = token_for(account)
    if not token:
        return "missing_token"
    if is_sealed(token):
        return "token_expired"
    try:
        if jwtutil.is_expired(token):
            return "token_expired"
    except jwtutil.JwtError:
        return "bad_token"
    return "ok"


def session_is_usable(token: str | None) -> bool:
    """True when the token can be sent to Cursor as-is, without a refresh."""
    if not token or is_sealed(token):
        return False
    try:
        return not jwtutil.is_expired(token, skew_seconds=120)
    except jwtutil.JwtError:
        return False


def apply_refreshed_tokens(bundle: SessionBundle, result: usage.RefreshResult) -> SessionBundle:
    if result.access_token:
        bundle.set("cursorAuth/accessToken", result.access_token)
    if result.refresh_token:
        bundle.set("cursorAuth/refreshToken", result.refresh_token)
    return bundle


def refresh_bundle(bundle: SessionBundle) -> tuple[SessionBundle, str]:
    refresh_token = bundle.get("cursorAuth/refreshToken")
    # Ciphertext is not a refresh token. Posting it makes Cursor answer
    # shouldLogout and the account looks revoked while the IDE is still using it.
    if not refresh_token or is_sealed(refresh_token):
        return bundle, "token_expired"
    result = usage.refresh_session(refresh_token)
    if result.should_logout or result.error == "needs_reauth":
        return bundle, "needs_reauth"
    if not result.ok:
        return bundle, "token_expired"
    return apply_refreshed_tokens(bundle, result), "ok"


def snapshot_for_account(account: Account) -> tuple[UsageSnapshot, str]:
    try:
        bundle = store.load_bundle(account.id)
    except StoreError:
        return UsageSnapshot(fetched_at=isoformat(utcnow()) or "", error="missing_token"), "missing_token"

    access = bundle.get("cursorAuth/accessToken")
    status = "ok"
    if is_sealed(access):
        return UsageSnapshot(fetched_at=isoformat(utcnow()) or "", error="token_expired"), "token_expired"
    if not access:
        status = "missing_token"
    else:
        try:
            if jwtutil.is_expired(access, skew_seconds=120):
                status = "token_expired"
        except jwtutil.JwtError:
            status = "bad_token"

    if status != "ok":
        bundle, status = refresh_bundle(bundle)
        if status == "ok":
            store.save_bundle(account.id, bundle)
            access = bundle.get("cursorAuth/accessToken")

    if not access or status not in {"ok"}:
        return UsageSnapshot(fetched_at=isoformat(utcnow()) or "", error=status), status

    snap = usage.snapshot_for_token(access)
    if snap.error == "token_expired":
        bundle, status = refresh_bundle(bundle)
        if status != "ok":
            return UsageSnapshot(fetched_at=isoformat(utcnow()) or "", error=status), status
        store.save_bundle(account.id, bundle)
        access = bundle.get("cursorAuth/accessToken")
        if not access:
            return UsageSnapshot(fetched_at=isoformat(utcnow()) or "", error="token_expired"), "token_expired"
        snap = usage.snapshot_for_token(access)
        if snap.error == "token_expired":
            return snap, "token_expired"
    if snap.error:
        return snap, "usage_error"
    return snap, "ok"


def merge_usage(previous: UsageSnapshot | None, incoming: UsageSnapshot) -> UsageSnapshot:
    if incoming.error and previous and not previous.error:
        return previous
    return incoming


def collect_rows(*, refresh: bool = True) -> list[AccountRow]:
    sync_live_into_active()
    index = store.load_index()
    try:
        live = read_live()
    except ServiceError:
        live = None
    live_id = match_live_account(index, live) if live else None
    cache = store.load_usage_cache()
    rows: list[AccountRow] = []
    fetch_ids: list[str] = []
    for account in index.sorted_accounts():
        status = token_status(account)
        rows.append(
            AccountRow(
                account=account,
                active=account.id == (live_id or index.active_id),
                live=account.id == live_id,
                usage=cache.get(account.id),
                token_status=status,
            )
        )
        if refresh:
            fetch_ids.append(account.id)

    if fetch_ids:
        fetched: dict[str, tuple[UsageSnapshot, str]] = {}
        with ThreadPoolExecutor(max_workers=min(6, len(fetch_ids))) as pool:
            futures = {
                pool.submit(snapshot_for_account, index.by_id(account_id)): account_id
                for account_id in fetch_ids
                if index.by_id(account_id)
            }
            for future in as_completed(futures):
                fetched[futures[future]] = future.result()
        for row in rows:
            if row.account.id not in fetched:
                continue
            snap, status = fetched[row.account.id]
            row.token_status = status
            row.usage = merge_usage(cache.get(row.account.id), snap)
            if not snap.error:
                cache[row.account.id] = snap
                if snap.membership_type:
                    row.account.membership_type = snap.membership_type
            elif row.account.id not in cache and snap.error:
                cache[row.account.id] = snap
        store.save_usage_cache(cache)
        store.save_index(index)
    return rows


def switch_to(
    target: str | None,
    *,
    strategy: str = "next",
    restart: bool | None = None,
    force: bool = False,
) -> tuple[Account | None, Account]:
    index = store.load_index()
    if not index.accounts:
        raise ServiceError("No saved accounts. Run `cuswap add` first.")
    sync_live_into_active()
    index = store.load_index()
    current_id = match_live_account(index) or index.active_id
    current = index.by_id(current_id) if current_id else None
    if current:
        snap, _status = snapshot_for_account(current)
        if not snap.error:
            cache = store.load_usage_cache()
            cache[current.id] = snap
            store.save_usage_cache(cache)

    if target:
        dest = store.resolve_account(index, target)
    else:
        dest = pick_account(index, current, strategy=strategy)
    if current and dest.id == current.id and not force:
        raise ServiceError(f"Already on {dest.email}. Pass --force to rewrite the session.")

    try:
        bundle = store.load_bundle(dest.id)
    except StoreError as exc:
        raise ServiceError(str(exc)) from exc
    access = bundle.get("cursorAuth/accessToken")
    if session_is_usable(access):
        # A still-valid session JWT must be written as-is. Refreshing it rotates
        # the token Cursor is about to encrypt, and the next list used to treat
        # that ciphertext as a revoked session.
        token_state = "ok"
    else:
        bundle, token_state = refresh_bundle(bundle)
        if token_state == "ok":
            store.save_bundle(dest.id, bundle)
        elif token_state == "needs_reauth":
            raise ServiceError(
                f"Session for {dest.email} is no longer valid. "
                f"Run `cuswap login` and sign in as that account in the private window."
            )
        access = bundle.get("cursorAuth/accessToken")
    if is_sealed(access):
        raise ServiceError(
            f"Could not read the saved session for {dest.email}. "
            f"Run `cuswap login` and sign in as that account in the private window."
        )
    access = bundle.get("cursorAuth/accessToken")
    if access and not bundle.get("glass.lastSignedInAuthId"):
        try:
            bundle.set("glass.lastSignedInAuthId", jwtutil.subject(access))
        except jwtutil.JwtError:
            pass
    settings = store.load_settings()
    should_restart = settings["switch"]["restart"] if restart is None else restart
    index.active_id = dest.id
    store.save_index(index)
    running = cursor_proc.is_running()
    if running:
        # Killing Cursor from an integrated terminal also kills this process.
        # A breakaway helper finishes the write + relaunch after IDE exit.
        apply_switch.write_pending(
            {
                "account_id": dest.id,
                "email": dest.email,
                "restart": should_restart,
                "bundle": bundle.to_dict(),
            }
        )
        cursor_proc.spawn_detached(apply_switch.helper_argv())
        return current, dest
    try:
        cursor_db.write_bundle(bundle)
    except cursor_db.CursorDbError as exc:
        raise ServiceError(str(exc)) from exc
    if should_restart:
        cursor_proc.start_cursor()
    return current, dest


def pick_account(index: AccountIndex, current: Account | None, *, strategy: str) -> Account:
    rotatable = [item for item in index.sorted_accounts() if not item.disabled]
    if not rotatable:
        raise ServiceError("All accounts are disabled.")
    if strategy == "best":
        rows = collect_rows(refresh=True)
        usable = [row for row in rows if not row.account.disabled and row.token_status == "ok"]
        usable = [row for row in usable if not (row.usage and row.usage.exhausted)]
        if not usable:
            raise ServiceError("No account has remaining quota.")
        def remaining(row: AccountRow) -> float:
            if not row.usage:
                return -1.0
            if row.usage.auto_percent is not None:
                return 100.0 - float(row.usage.auto_percent)
            if row.usage.percent is not None:
                return 100.0 - float(row.usage.percent)
            if row.usage.remaining is not None:
                return float(row.usage.remaining)
            return -1.0
        usable.sort(key=remaining, reverse=True)
        return usable[0].account
    if strategy == "next-available":
        rows = {row.account.id: row for row in collect_rows(refresh=True)}
        start = 0
        if current:
            ids = [item.id for item in rotatable]
            if current.id in ids:
                start = (ids.index(current.id) + 1) % len(ids)
        for offset in range(len(rotatable)):
            candidate = rotatable[(start + offset) % len(rotatable)]
            row = rows.get(candidate.id)
            if row and row.token_status == "ok" and not (row.usage and row.usage.exhausted):
                return candidate
        raise ServiceError("No available account to switch to.")
    # next
    if not current:
        return rotatable[0]
    ids = [item.id for item in rotatable]
    if current.id not in ids:
        return rotatable[0]
    return rotatable[(ids.index(current.id) + 1) % len(ids)]


def remove_account(target: str) -> Account:
    index = store.load_index()
    account = store.resolve_account(index, target)
    index.accounts = [item for item in index.accounts if item.id != account.id]
    if index.active_id == account.id:
        index.active_id = index.accounts[0].id if index.accounts else None
    store.delete_bundle(account.id)
    store.save_index(index)
    return account


def set_disabled(target: str, disabled: bool) -> Account:
    index = store.load_index()
    account = store.resolve_account(index, target)
    account.disabled = disabled
    store.save_index(index)
    return account


def prepare_login(*, restart: bool = True) -> None:
    sync_live_into_active()
    running = cursor_proc.is_running()
    if running:
        cursor_proc.quit_cursor()
    cursor_db.clear_auth()
    if restart:
        cursor_proc.start_cursor()


def doctor() -> dict[str, object]:
    from cuswap.paths import data_dir, default_cursor_exe, state_db_path

    db = state_db_path()
    storage = cursor_db.inspect_storage(db)
    info: dict[str, object] = {
        "data_dir": str(data_dir()),
        "state_db": str(db),
        "state_db_exists": db.is_file(),
        "state_db_status": storage.get("status"),
        "wal_exists": storage.get("wal_exists"),
        "shm_exists": storage.get("shm_exists"),
        "backup_exists": storage.get("backup_exists"),
        "corrupted_files": storage.get("corrupted_files"),
        "cursor_running": cursor_proc.is_running(),
        "cursor_exe": str(default_cursor_exe() or ""),
        "accounts": len(store.load_index().accounts),
    }
    try:
        bundle = read_live()
        info["live_email"] = bundle.get("cursorAuth/cachedEmail")
        info["live_keys"] = len(bundle.keys)
    except ServiceError as exc:
        info["live_email"] = None
        info["live_error"] = str(exc)
    return info


def repair_storage() -> str:
    if cursor_proc.is_running():
        raise ServiceError("Quit Cursor before repairing state.vscdb.")
    try:
        return cursor_db.repair_state_db()
    except cursor_db.CursorDbError as exc:
        raise ServiceError(str(exc)) from exc
