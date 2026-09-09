"""Project queued media jobs into executed turn order for the shared compactor."""


def executed_media_rows(rows: list) -> list:
    inputs = [row for row in rows if row.role == "user" and (row.message_meta or {}).get("media_request")]
    if not inputs:
        return rows
    groups = {str(row.id): [row] for row in inputs}
    unowned = []
    for row in rows:
        if row in inputs:
            continue
        meta = row.message_meta or {}
        owner = str(meta.get("turn_anchor_id") or meta.get("subagent_turn_anchor_id") or "")
        if owner in groups:
            groups[owner].append(row)
        else:
            unowned.append(row)
    # Pending inputs are future jobs, not part of this provider conversation.
    # The input and every result are still durably retained in their original rows.
    return unowned + [item for row in inputs
                       if (row.message_meta or {}).get("subagent_input_state") != "pending"
                       for item in groups[str(row.id)]]
