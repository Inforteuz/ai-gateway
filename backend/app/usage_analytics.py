"""Aggregated key attribution; never exposes request text, arguments or key secrets."""
from datetime import datetime, timedelta

from .billing import TASHKENT
from .common import ApiError


def analytics(db, *, user_id=None, key_id=None, days=30, admin=False):
    days = max(1, min(days, 90))
    today = datetime.now(TASHKENT).replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = today - timedelta(days=days - 1), today + timedelta(days=1)
    if key_id is not None:
        owned = db.one("SELECT user_id FROM api_keys WHERE id=?", (key_id,))
        if owned is None or (user_id is not None and owned["user_id"] != user_id):
            raise ApiError(404, "key_not_found", "Kalit topilmadi.")
    where, args = ["u.created_at>=?", "u.created_at<?"], [int(start.timestamp()), int(end.timestamp())]
    if user_id is not None:
        where.append("u.user_id=?"); args.append(user_id)
    if key_id is not None:
        where.append("u.key_id=?"); args.append(key_id)
    cte = "WITH scoped AS (SELECT u.*, CAST(u.attribution AS jsonb) meta FROM usage_logs u WHERE " + " AND ".join(where) + ") "
    sums = """COUNT(*) requests, COALESCE(SUM(prompt_tokens),0) prompt_tokens,
        COALESCE(SUM(completion_tokens),0) completion_tokens, COALESCE(SUM(cost),0) cost,
        COALESCE(SUM(CASE WHEN estimated=1 OR meta->>'provider'='cluster' THEN prompt_tokens+completion_tokens ELSE 0 END),0) estimated_tokens"""
    totals = dict(db.one(cte + "SELECT " + sums + " FROM scoped", args))
    rows = db.all(cte + "SELECT to_char(to_timestamp(created_at) AT TIME ZONE 'Asia/Tashkent','YYYY-MM-DD') date, " + sums + " FROM scoped GROUP BY date ORDER BY date", args)
    by_date = {r["date"]: dict(r) for r in rows}
    daily = []
    for i in range(days):
        date = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        daily.append(by_date.get(date, {"date": date, "requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0, "estimated_tokens": 0}))
    # Comparison covers all keys in the scope even when one key is selected.
    # Unused active keys show zero; revoked keys retain historical usage.
    key_where, key_args = [], [int(start.timestamp()), int(end.timestamp())]
    if user_id is not None:
        key_where.append("k.user_id=?"); key_args.append(user_id)
    keys = db.all("""SELECT k.id, k.user_id, k.name, k.status, us.email,
        COUNT(u.id) requests, COALESCE(SUM(u.prompt_tokens),0) prompt_tokens,
        COALESCE(SUM(u.completion_tokens),0) completion_tokens, COALESCE(SUM(u.cost),0) cost,
        COALESCE(SUM(CASE WHEN u.estimated=1 OR CAST(u.attribution AS jsonb)->>'provider'='cluster' THEN u.prompt_tokens+u.completion_tokens ELSE 0 END),0) estimated_tokens
        FROM api_keys k JOIN users us ON us.id=k.user_id
        LEFT JOIN usage_logs u ON u.key_id=k.id AND u.user_id=k.user_id AND u.created_at>=? AND u.created_at<? """
        + ("WHERE " + " AND ".join(key_where) if key_where else "")
        + " GROUP BY k.id, us.email HAVING k.status='active' OR COUNT(u.id)>0 OR k.id=? ORDER BY SUM(u.prompt_tokens+u.completion_tokens) DESC NULLS LAST, k.id", key_args + [key_id])
    key_items = []
    for r in keys:
        item = dict(r)
        if not admin:
            item.pop("email", None); item.pop("user_id", None)
        key_items.append(item)
    dimensions = {}
    for dimension, expr in (("model", "model"), ("project", "meta->>'project'"),
                            ("purpose", "meta->>'purpose'"), ("client", "meta->>'client'")):
        dimensions[dimension] = [dict(r) for r in db.all(cte + "SELECT COALESCE(NULLIF(" + expr + ",''),'') label, " + sums
            + " FROM scoped GROUP BY label ORDER BY SUM(prompt_tokens+completion_tokens) DESC, label LIMIT 50", args)]
    dimensions["tool"] = [dict(r) for r in db.all(cte + "SELECT COALESCE(t.tool,'') label, " + sums + """ FROM scoped
        LEFT JOIN LATERAL (SELECT DISTINCT value tool FROM jsonb_array_elements_text(
            CASE WHEN jsonb_typeof(meta->'called_tools')='array' THEN meta->'called_tools' ELSE '[]'::jsonb END)) t ON TRUE
        GROUP BY t.tool ORDER BY SUM(prompt_tokens+completion_tokens) DESC, label LIMIT 50""", args)]
    return {"days": days, "key_id": key_id, "totals": totals, "keys": key_items, "daily": daily,
            "dimensions": dimensions, "tool_tokens_additive": False}
