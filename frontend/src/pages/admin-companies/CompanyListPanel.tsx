import { IconFilter } from "@tabler/icons-react";
import { ShieldCheck } from "lucide-react";
import Pagination from "../../components/Pagination";
import { formatDate, formatTokens } from "./helpers";
import { PAGE_SIZE } from "./model";

export default function CompanyListPanel(props: any) {
  const {
    t,
    showCreate,
    setShowCreate,
    setCreatedCode,
    newName,
    setNewName,
    handleCreate,
    creating,
    loading,
    error,
    statusDropdownRef,
    showStatusDropdown,
    setShowStatusDropdown,
    statusFilter,
    setStatusFilter,
    columns,
    handleSort,
    thStyle,
    SortArrow,
    gridCols,
    paged,
    setEditingCompany,
    handleViewCodes,
    handleToggle,
    setDeleteConfirmCompany,
    sorted,
    totalPages,
    page,
    setPage,
  } = props;

  return (
    <>
      {/* Create Company button */}
      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          marginBottom: "16px",
        }}
      >
        <button
          className="btn btn-primary"
          onClick={() => {
            setShowCreate(true);
            setCreatedCode("");
          }}
        >
          + {t("admin.createCompany", "Create Company")}
        </button>
      </div>

      {/* Create Company — inline input */}
      {showCreate && (
        <div
          className="card"
          style={{
            padding: "16px",
            marginBottom: "16px",
            border: "1px solid var(--accent-primary)",
          }}
        >
          <div
            style={{ fontSize: "13px", fontWeight: 600, marginBottom: "12px" }}
          >
            {t("admin.createCompany", "Create Company")}
          </div>
          <div style={{ display: "flex", gap: "8px" }}>
            <input
              className="form-input"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder={t("admin.companyNamePlaceholder", "Company name")}
              onKeyDown={(e) => e.key === "Enter" && handleCreate()}
              style={{ flex: 1 }}
              autoFocus
            />
            <button
              className="btn btn-primary"
              onClick={handleCreate}
              disabled={creating || !newName.trim()}
            >
              {creating ? "..." : t("common.create", "Create")}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => setShowCreate(false)}
            >
              {t("common.cancel", "Cancel")}
            </button>
          </div>
        </div>
      )}

      {/* Company List */}
      <div
        className="card"
        style={{
          padding: "0",
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "column",
          overflow: "visible",
        }}
      >
        {/* Table Header */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: gridCols,
            gap: "12px",
            padding: "10px 16px",
            fontSize: "11px",
            fontWeight: 600,
            color: "var(--text-tertiary)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            borderBottom: "1px solid var(--border-subtle)",
            background: "var(--bg-secondary)",
            borderRadius: "var(--radius-lg) var(--radius-lg) 0 0",
            flexShrink: 0,
            position: "relative",
            zIndex: 10,
          }}
        >
          {columns.map((col: any) => (
            <div
              key={col.key}
              style={thStyle}
              onClick={() => handleSort(col.key)}
            >
              {col.label}
              <SortArrow col={col.key} />
            </div>
          ))}
          <div
            ref={statusDropdownRef}
            style={{
              position: "relative",
              display: "flex",
              alignItems: "center",
              gap: "4px",
            }}
          >
            {t("admin.status", "Status")}
            <button
              onClick={() => setShowStatusDropdown((v: any) => !v)}
              style={{
                background: "none",
                border: "none",
                padding: "2px",
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                borderRadius: "4px",
                color:
                  statusFilter !== "all"
                    ? "var(--accent-primary)"
                    : "var(--text-tertiary)",
                transition: "color 0.15s",
              }}
              title={t("admin.filterStatus", "Filter by status")}
            >
              <IconFilter
                size={14}
                stroke={statusFilter !== "all" ? 2.5 : 1.8}
              />
            </button>
            {showStatusDropdown && (
              <div
                style={{
                  position: "absolute",
                  top: "100%",
                  left: 0,
                  marginTop: "4px",
                  background: "var(--bg-primary)",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "8px",
                  boxShadow: "0 8px 24px rgba(0,0,0,0.12)",
                  zIndex: 100,
                  minWidth: "120px",
                  padding: "4px",
                  overflow: "hidden",
                }}
              >
                {(["all", "active", "disabled"] as const).map((val) => (
                  <div
                    key={val}
                    onClick={() => {
                      setStatusFilter(val);
                      setPage(0);
                      setShowStatusDropdown(false);
                    }}
                    style={{
                      padding: "6px 10px",
                      fontSize: "12px",
                      cursor: "pointer",
                      borderRadius: "6px",
                      transition: "background 0.1s",
                      color:
                        statusFilter === val
                          ? "var(--accent-primary)"
                          : "var(--text-secondary)",
                      fontWeight: statusFilter === val ? 600 : 400,
                      background:
                        statusFilter === val
                          ? "var(--bg-secondary)"
                          : "transparent",
                    }}
                    onMouseEnter={(e) => {
                      if (statusFilter !== val)
                        e.currentTarget.style.background =
                          "var(--bg-secondary)";
                    }}
                    onMouseLeave={(e) => {
                      if (statusFilter !== val)
                        e.currentTarget.style.background = "transparent";
                    }}
                  >
                    {val === "all"
                      ? t("admin.all", "All")
                      : val === "active"
                        ? t("admin.active", "Active")
                        : t("admin.disabled", "Disabled")}
                  </div>
                ))}
              </div>
            )}
          </div>
          <div>{t("admin.action", "Action")}</div>
        </div>

        {/* Scrollable table body */}
        <div style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
          {loading && (
            <div
              style={{
                textAlign: "center",
                padding: "40px",
                color: "var(--text-tertiary)",
                fontSize: "13px",
              }}
            >
              {t("common.loading", "Loading...")}
            </div>
          )}

          {error && (
            <div
              style={{
                textAlign: "center",
                padding: "24px",
                color: "var(--error)",
                fontSize: "13px",
              }}
            >
              {error}
            </div>
          )}

          {!loading &&
            paged.map((c: any) => (
              <div
                key={c.id}
                style={{
                  display: "grid",
                  gridTemplateColumns: gridCols,
                  gap: "12px",
                  padding: "12px 16px",
                  alignItems: "center",
                  borderBottom: "1px solid var(--border-subtle)",
                  fontSize: "13px",
                  opacity: c.is_active ? 1 : 0.5,
                }}
              >
                <div>
                  <div
                    style={{
                      fontWeight: 500,
                      display: "flex",
                      alignItems: "center",
                      gap: "6px",
                    }}
                  >
                    {c.name}
                    {c.is_default && (
                      <span
                        style={{
                          fontSize: "10px",
                          fontWeight: 600,
                          padding: "1px 5px",
                          borderRadius: "4px",
                          background: "rgba(59,130,246,0.1)",
                          color: "var(--accent-primary)",
                          border: "1px solid rgba(59,130,246,0.2)",
                          lineHeight: "16px",
                          flexShrink: 0,
                        }}
                      >
                        {t("admin.default", "Default")}
                      </span>
                    )}
                  </div>
                  <div
                    style={{
                      fontSize: "11px",
                      color: "var(--text-tertiary)",
                      fontFamily: "monospace",
                    }}
                  >
                    {c.slug}
                  </div>
                </div>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    alignItems: "center",
                    gap: "2px",
                  }}
                >
                  {c.sso_enabled ? (
                    <>
                      <span
                        style={{
                          color: "var(--accent-primary)",
                          fontSize: "14px",
                        }}
                        title="SSO Enabled"
                      >
                        <ShieldCheck size={14} />
                      </span>
                      {c.sso_domain && (
                        <span
                          style={{
                            fontSize: "9px",
                            background: "rgba(59,130,246,0.1)",
                            color: "var(--accent-primary)",
                            padding: "1px 4px",
                            borderRadius: "4px",
                            maxWidth: "100px",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {c.sso_domain}
                        </span>
                      )}
                    </>
                  ) : (
                    <span
                      style={{ color: "var(--text-tertiary)", opacity: 0.3 }}
                      title="SSO Disabled"
                    >
                      —
                    </span>
                  )}
                </div>
                <div
                  style={{
                    fontSize: "12px",
                    color: c.org_admin_email
                      ? "var(--text-primary)"
                      : "var(--text-tertiary)",
                  }}
                >
                  {c.org_admin_email || "-"}
                </div>
                <div>{c.user_count ?? "-"}</div>
                <div>{c.agent_count ?? "-"}</div>
                <div
                  style={{ fontSize: "12px", fontFamily: "var(--font-mono)" }}
                >
                  {formatTokens(c.total_tokens)}
                </div>
                <div
                  style={{ fontSize: "12px", color: "var(--text-secondary)" }}
                >
                  {formatDate(c.created_at)}
                </div>
                <div
                  style={{ display: "flex", alignItems: "center", gap: "4px" }}
                >
                  <span
                    className={`badge ${c.is_active ? "badge-success" : "badge-error"}`}
                    style={{ fontSize: "10px" }}
                  >
                    {c.is_active
                      ? t("admin.active", "Active")
                      : t("admin.disabled", "Disabled")}
                  </span>
                </div>
                <div style={{ display: "flex", gap: "4px" }}>
                  <button
                    className="btn btn-ghost"
                    style={{
                      padding: "2px 8px",
                      fontSize: "11px",
                      height: "24px",
                      color: "var(--accent-primary)",
                    }}
                    onClick={() => setEditingCompany(c)}
                  >
                    {t("admin.edit", "Edit")}
                  </button>
                  <button
                    className="btn btn-ghost"
                    style={{
                      padding: "2px 8px",
                      fontSize: "11px",
                      height: "24px",
                      color: "var(--text-secondary)",
                    }}
                    onClick={() => handleViewCodes(c.id, c.name)}
                  >
                    邀请码
                  </button>
                  <button
                    className="btn btn-ghost"
                    style={{
                      padding: "2px 8px",
                      fontSize: "11px",
                      height: "24px",
                      color: c.is_default
                        ? "var(--text-tertiary)"
                        : c.is_active
                          ? "var(--error)"
                          : "var(--success)",
                      cursor: c.is_default ? "not-allowed" : "pointer",
                      opacity: c.is_default ? 0.5 : 1,
                    }}
                    onClick={() => handleToggle(c.id, !!c.is_active)}
                    disabled={c.is_default}
                    title={
                      c.is_default
                        ? t(
                            "admin.cannotDisableDefault",
                            "Cannot disable the default company — platform admin would be locked out",
                          )
                        : undefined
                    }
                  >
                    {c.is_active
                      ? t("admin.disable", "Disable")
                      : t("admin.enable", "Enable")}
                  </button>
                  <button
                    className="btn btn-ghost"
                    style={{
                      padding: "2px 8px",
                      fontSize: "11px",
                      height: "24px",
                      color: c.is_default
                        ? "var(--text-tertiary)"
                        : "var(--error)",
                      cursor: c.is_default ? "not-allowed" : "pointer",
                      opacity: c.is_default ? 0.4 : 1,
                    }}
                    onClick={() => !c.is_default && setDeleteConfirmCompany(c)}
                    disabled={c.is_default}
                    title={
                      c.is_default
                        ? t(
                            "admin.cannotDeleteDefault",
                            "Cannot delete the default company",
                          )
                        : t("admin.deleteCompany", "Delete Company")
                    }
                  >
                    {t("admin.delete", "Delete")}
                  </button>
                </div>
              </div>
            ))}

          {!loading && paged.length === 0 && !error && (
            <div
              style={{
                textAlign: "center",
                padding: "40px",
                color: "var(--text-tertiary)",
                fontSize: "13px",
              }}
            >
              {statusFilter !== "all"
                ? t(
                    "admin.noFilterResults",
                    "No companies match the current filter.",
                  )
                : t("common.noData", "No data")}
            </div>
          )}
        </div>

        {/* Pagination */}
        {!loading && totalPages > 1 && (
          <Pagination
            page={page + 1}
            pageSize={PAGE_SIZE}
            total={sorted.length}
            onPageChange={(nextPage) => setPage(nextPage - 1)}
          />
        )}
      </div>
    </>
  );
}
