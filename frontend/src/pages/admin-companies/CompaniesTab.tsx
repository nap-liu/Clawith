import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { adminApi } from "../../services/api";
import { useDialog } from "../../components/Dialog/DialogProvider";
import { fetchJson } from "./helpers";
import { PAGE_SIZE, type SortDir, type SortKey } from "./model";
import CompanyManagementModals from "./CompanyManagementModals";
import CompanyListPanel from "./CompanyListPanel";

export default function CompaniesTab() {
  const { t } = useTranslation();
  const dialog = useDialog();
  const [companies, setCompanies] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Sorting
  const [sortKey, setSortKey] = useState<SortKey>("created_at");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Status filter
  const [statusFilter, setStatusFilter] = useState<
    "all" | "active" | "disabled"
  >("all");
  const [showStatusDropdown, setShowStatusDropdown] = useState(false);
  const statusDropdownRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (
        statusDropdownRef.current &&
        !statusDropdownRef.current.contains(e.target as Node)
      ) {
        setShowStatusDropdown(false);
      }
    };
    if (showStatusDropdown) document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [showStatusDropdown]);

  // Pagination
  const [page, setPage] = useState(0);

  // Create company
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [createdCode, setCreatedCode] = useState("");
  const [createdCompanyName, setCreatedCompanyName] = useState("");

  // Edit company modal
  const [editingCompany, setEditingCompany] = useState<any>(null);
  const [deletingCompanyId, setDeletingCompanyId] = useState<string | null>(
    null,
  );
  const [deleteConfirmCompany, setDeleteConfirmCompany] = useState<any>(null);
  const [publicBaseUrl, setPublicBaseUrl] = useState("");

  // Invitation codes modal
  const [codesModal, setCodesModal] = useState<{
    companyId: string;
    companyName: string;
  } | null>(null);
  const [companyCodes, setCompanyCodes] = useState<any[]>([]);
  const [loadingCodes, setLoadingCodes] = useState(false);
  const [newlyGeneratedCode, setNewlyGeneratedCode] = useState("");
  const [codeCopied2, setCodeCopied2] = useState(false);

  // Toast
  const [toast, setToast] = useState<{
    msg: string;
    type: "success" | "error";
  } | null>(null);
  const showToast = (msg: string, type: "success" | "error" = "success") => {
    setToast({ msg, type });
    setTimeout(() => setToast(null), 3000);
  };

  const loadCompanies = async () => {
    setLoading(true);
    try {
      const data = await adminApi.listCompanies();
      setCompanies(data);
    } catch (e: any) {
      setError(e.message);
    }
    setLoading(false);
  };

  useEffect(() => {
    loadCompanies();
    fetchJson<any>("/enterprise/system-settings/platform")
      .then((d) => {
        if (d.value?.public_base_url) setPublicBaseUrl(d.value.public_base_url);
      })
      .catch(() => {});
  }, []);

  // Sorting logic
  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    }
    setPage(0);
  };

  const sorted = useMemo(() => {
    let list = [...companies];
    if (statusFilter === "active") list = list.filter((c) => c.is_active);
    else if (statusFilter === "disabled")
      list = list.filter((c) => !c.is_active);
    list.sort((a, b) => {
      let av = a[sortKey],
        bv = b[sortKey];
      if (sortKey === "name" || sortKey === "org_admin_email") {
        av = (av || "").toLowerCase();
        bv = (bv || "").toLowerCase();
      }
      if (sortKey === "created_at") {
        av = av ? new Date(av).getTime() : 0;
        bv = bv ? new Date(bv).getTime() : 0;
      }
      if (av < bv) return sortDir === "asc" ? -1 : 1;
      if (av > bv) return sortDir === "asc" ? 1 : -1;
      return 0;
    });
    return list;
  }, [companies, sortKey, sortDir, statusFilter]);

  // Pagination
  const totalPages = Math.ceil(sorted.length / PAGE_SIZE);
  const paged = sorted.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);

  useEffect(() => {
    const lastPage = Math.max(0, totalPages - 1);
    if (page > lastPage) setPage(lastPage);
  }, [page, totalPages]);

  const handleCreate = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    try {
      const result = await adminApi.createCompany({ name: newName.trim() });
      setCreatedCompanyName(newName.trim());
      setCreatedCode(result.admin_invitation_code || "");
      setNewName("");
      setShowCreate(false);
      loadCompanies();
    } catch (e: any) {
      showToast(e.message || "Failed", "error");
    }
    setCreating(false);
  };

  const handleViewCodes = async (companyId: string, companyName: string) => {
    setCodesModal({ companyId, companyName });
    setNewlyGeneratedCode("");
    setLoadingCodes(true);
    try {
      const res = await adminApi.listCompanyCodes(companyId);
      setCompanyCodes(res.codes || []);
    } catch {
      setCompanyCodes([]);
    }
    setLoadingCodes(false);
  };

  const handleGenerateCode = async () => {
    if (!codesModal) return;
    try {
      const res = await adminApi.createCompanyCode(codesModal.companyId);
      setNewlyGeneratedCode(res.code || "");
      setCodeCopied2(false);
      // Refresh list
      const listRes = await adminApi.listCompanyCodes(codesModal.companyId);
      setCompanyCodes(listRes.codes || []);
    } catch (e: any) {
      showToast(e.message || "Failed", "error");
    }
  };

  const handleToggle = async (id: string, currentlyActive: boolean) => {
    const action = currentlyActive ? "disable" : "enable";
    if (currentlyActive) {
      const ok = await dialog.confirm(
        t("common.dialog.disableCompanyConfirm"),
        {
          title: t("common.dialog.disableCompany"),
          danger: true,
          confirmLabel: t("common.confirmActions.disableLabel"),
        },
      );
      if (!ok) return;
    }
    try {
      await adminApi.toggleCompany(id);
      loadCompanies();
      showToast(`Company ${action}d`);
    } catch (e: any) {
      showToast(e.message || "Failed", "error");
    }
  };

  const handleDelete = async (company: any) => {
    setDeletingCompanyId(company.id);
    try {
      await adminApi.deleteCompany(company.id);
      loadCompanies();
      showToast(`Company "${company.name}" deleted`);
    } catch (e: any) {
      showToast(e.message || "Failed to delete", "error");
    }
    setDeletingCompanyId(null);
    setDeleteConfirmCompany(null);
  };

  // Sort indicator arrow
  const SortArrow = ({ col }: { col: SortKey }) => {
    if (sortKey !== col)
      return <span style={{ opacity: 0.3, marginLeft: "2px" }}>&#x2195;</span>;
    return (
      <span style={{ marginLeft: "2px" }}>
        {sortDir === "asc" ? "\u2191" : "\u2193"}
      </span>
    );
  };

  // Column header style
  const thStyle: React.CSSProperties = {
    cursor: "pointer",
    userSelect: "none",
    display: "flex",
    alignItems: "center",
    gap: "2px",
  };

  const columns: { key: SortKey; label: string; flex: string }[] = [
    { key: "name", label: t("admin.company", "Company"), flex: "2fr" },
    { key: "sso_enabled", label: "SSO", flex: "100px" },
    {
      key: "org_admin_email",
      label: t("admin.orgAdmin", "Admin Email"),
      flex: "1.5fr",
    },
    { key: "user_count", label: t("admin.users", "Users"), flex: "70px" },
    { key: "agent_count", label: t("admin.agents", "Agents"), flex: "70px" },
    {
      key: "total_tokens",
      label: t("admin.tokens", "Token Usage"),
      flex: "100px",
    },
    {
      key: "created_at",
      label: t("admin.createdAt", "Created"),
      flex: "100px",
    },
    { key: "is_active", label: t("admin.status", "Status"), flex: "100px" },
  ];
  const actionColFlex = "180px";

  const gridCols = columns.map((c) => c.flex).join(" ") + " " + actionColFlex;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        flex: 1,
        minHeight: 0,
      }}
    >
      {toast && (
        <div
          style={{
            position: "fixed",
            top: "20px",
            right: "20px",
            padding: "10px 20px",
            borderRadius: "8px",
            background:
              toast.type === "success" ? "var(--success)" : "var(--error)",
            color: "#fff",
            fontSize: "13px",
            zIndex: 9999,
            boxShadow: "0 4px 12px rgba(0,0,0,0.2)",
          }}
        >
          {toast.msg}
        </div>
      )}

      <CompanyManagementModals
        t={t}
        editingCompany={editingCompany}
        publicBaseUrl={publicBaseUrl}
        setEditingCompany={setEditingCompany}
        loadCompanies={loadCompanies}
        deleteConfirmCompany={deleteConfirmCompany}
        setDeleteConfirmCompany={setDeleteConfirmCompany}
        handleDelete={handleDelete}
        deletingCompanyId={deletingCompanyId}
        createdCode={createdCode}
        createdCompanyName={createdCompanyName}
        setCreatedCode={setCreatedCode}
        codesModal={codesModal}
        setCodesModal={setCodesModal}
        newlyGeneratedCode={newlyGeneratedCode}
        setNewlyGeneratedCode={setNewlyGeneratedCode}
        handleGenerateCode={handleGenerateCode}
        loadingCodes={loadingCodes}
        companyCodes={companyCodes}
        codeCopied2={codeCopied2}
        setCodeCopied2={setCodeCopied2}
      />

      <CompanyListPanel
        t={t}
        showCreate={showCreate}
        setShowCreate={setShowCreate}
        setCreatedCode={setCreatedCode}
        newName={newName}
        setNewName={setNewName}
        handleCreate={handleCreate}
        creating={creating}
        loading={loading}
        error={error}
        statusDropdownRef={statusDropdownRef}
        showStatusDropdown={showStatusDropdown}
        setShowStatusDropdown={setShowStatusDropdown}
        statusFilter={statusFilter}
        setStatusFilter={setStatusFilter}
        columns={columns}
        handleSort={handleSort}
        thStyle={thStyle}
        SortArrow={SortArrow}
        gridCols={gridCols}
        paged={paged}
        setEditingCompany={setEditingCompany}
        handleViewCodes={handleViewCodes}
        handleToggle={handleToggle}
        setDeleteConfirmCompany={setDeleteConfirmCompany}
        sorted={sorted}
        totalPages={totalPages}
        page={page}
        setPage={setPage}
      />
    </div>
  );
}
