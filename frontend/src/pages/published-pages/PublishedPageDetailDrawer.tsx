import {
  IconCopy,
  IconExternalLink,
  IconHistory,
  IconLock,
  IconSettings,
  IconTrash,
  IconUsers,
  IconWorld,
  IconX,
} from "@tabler/icons-react";

import OrgMemberAccessPicker, {
  type AgentAccessUser,
} from "../../components/OrgMemberAccessPicker";
import Pagination from "../../components/Pagination";
import PublishedPageAttribution from "../../components/PublishedPageAttribution";
import {
  VISITOR_PAGE_SIZE,
  formatTime,
  type AccessMode,
  type AccessUser,
  type Paged,
  type PublishedPageDetail,
  type Visitor,
} from "./model";

interface PublishedPageDetailDrawerProps {
  selected: PublishedPageDetail;
  updateSearch: (updates: Record<string, string | null>) => void;
  absolutePageUrl: (url: string) => string;
  copyPageUrl: (url: string) => Promise<void>;
  setShowDeleteConfirm: (show: boolean) => void;
  activeTab: "permissions" | "visitors";
  setActiveTab: (tab: "permissions" | "visitors") => void;
  mode: AccessMode;
  setMode: (mode: AccessMode) => void;
  selectedPeople: AgentAccessUser[];
  setSelectedPeople: (users: AgentAccessUser[]) => void;
  showMemberPicker: boolean;
  setShowMemberPicker: (show: boolean) => void;
  pendingUsers: AccessUser[];
  resolveRequest: (
    user: AccessUser,
    status: "approved" | "rejected",
  ) => Promise<void>;
  saving: boolean;
  dirty: boolean;
  save: () => Promise<void>;
  visitorsLoading: boolean;
  visitorData?: Paged<Visitor>;
  visitorPage: number;
  setVisitorPage: (page: number) => void;
}

export default function PublishedPageDetailDrawer({
  selected,
  updateSearch,
  absolutePageUrl,
  copyPageUrl,
  setShowDeleteConfirm,
  activeTab,
  setActiveTab,
  mode,
  setMode,
  selectedPeople,
  setSelectedPeople,
  showMemberPicker,
  setShowMemberPicker,
  pendingUsers,
  resolveRequest,
  saving,
  dirty,
  save,
  visitorsLoading,
  visitorData,
  visitorPage,
  setVisitorPage,
}: PublishedPageDetailDrawerProps) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 2000,
        background: "rgba(0,0,0,.28)",
      }}
      onClick={() => updateSearch({ page: null })}
    >
      <aside
        style={{
          position: "absolute",
          right: 0,
          top: 0,
          bottom: 0,
          width: "min(560px, 94vw)",
          background: "var(--bg-primary)",
          padding: 24,
          overflowY: "auto",
          boxShadow: "-12px 0 40px rgba(0,0,0,.16)",
        }}
        onClick={(event) => event.stopPropagation()}
      >
        <button
          aria-label="关闭"
          onClick={() => updateSearch({ page: null })}
          style={{
            float: "right",
            border: 0,
            background: "none",
            color: "inherit",
            cursor: "pointer",
          }}
        >
          <IconX size={20} />
        </button>
        <h2 style={{ fontSize: 19, margin: "0 0 10px" }}>
          {selected.title || selected.source_path}
        </h2>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "8px 10px",
            border: "1px solid var(--border-subtle)",
            borderRadius: 7,
            background: "var(--bg-secondary)",
          }}
        >
          <a
            href={absolutePageUrl(selected.url)}
            target="_blank"
            rel="noreferrer"
            title={absolutePageUrl(selected.url)}
            style={{
              minWidth: 0,
              flex: 1,
              fontSize: 12,
              color: "var(--text-secondary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              textDecoration: "none",
            }}
          >
            {absolutePageUrl(selected.url)}
          </a>
          <button
            type="button"
            aria-label="复制发布地址"
            title="复制发布地址"
            onClick={() => void copyPageUrl(selected.url)}
            style={{
              border: 0,
              background: "transparent",
              color: "var(--text-secondary)",
              padding: 3,
              cursor: "pointer",
              display: "inline-flex",
            }}
          >
            <IconCopy size={15} />
          </button>
          <a
            href={absolutePageUrl(selected.url)}
            target="_blank"
            rel="noreferrer"
            aria-label="打开发布页面"
            title="打开发布页面"
            style={{
              color: "var(--text-secondary)",
              display: "inline-flex",
            }}
          >
            <IconExternalLink size={15} />
          </a>
        </div>
        <PublishedPageAttribution
          createdBy={selected.created_by}
          createdAt={selected.created_at}
          lastPublishedBy={selected.last_published_by}
          lastPublishedAt={selected.last_published_at}
          variant="detail"
        />
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            marginTop: 10,
          }}
        >
          <button
            type="button"
            className="btn btn-danger btn-sm"
            onClick={() => setShowDeleteConfirm(true)}
          >
            <IconTrash size={14} /> 删除发布地址
          </button>
        </div>

        <div
          style={{
            display: "flex",
            gap: 4,
            borderBottom: "1px solid var(--border-subtle)",
            marginTop: 24,
          }}
        >
          {(
            [
              ["permissions", "权限设置", IconSettings],
              [
                "visitors",
                `访问记录 ${selected.visitor_count}`,
                IconHistory,
              ],
            ] as const
          ).map(([value, label, Icon]) => (
            <button
              key={value}
              type="button"
              onClick={() => setActiveTab(value)}
              style={{
                border: 0,
                borderBottom:
                  activeTab === value
                    ? "2px solid var(--accent-primary)"
                    : "2px solid transparent",
                background: "none",
                color:
                  activeTab === value
                    ? "var(--text-primary)"
                    : "var(--text-secondary)",
                padding: "10px 12px",
                cursor: "pointer",
                display: "inline-flex",
                gap: 6,
                alignItems: "center",
                fontWeight: 600,
              }}
            >
              <Icon size={15} /> {label}
            </button>
          ))}
        </div>

        {activeTab === "permissions" ? (
          <>
            <div style={{ display: "grid", gap: 8, marginTop: 18 }}>
              {(
                [
                  ["public", "公开", "任何人无需登录即可访问", IconWorld],
                  [
                    "authenticated",
                    "仅登录",
                    "公司内已登录用户可访问",
                    IconLock,
                  ],
                  [
                    "restricted",
                    "指定人员",
                    "仅发布者和指定人员可访问",
                    IconUsers,
                  ],
                ] as const
              ).map(([value, title, description, Icon]) => (
                <label
                  key={value}
                  style={{
                    padding: 12,
                    border: `1px solid ${mode === value ? "var(--accent-primary)" : "var(--border-subtle)"}`,
                    borderRadius: 8,
                    display: "flex",
                    gap: 10,
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="radio"
                    checked={mode === value}
                    onChange={() => setMode(value)}
                  />
                  <Icon size={18} />
                  <span>
                    <strong style={{ display: "block", fontSize: 13 }}>
                      {title}
                    </strong>
                    <span
                      style={{
                        fontSize: 12,
                        color: "var(--text-tertiary)",
                      }}
                    >
                      {description}
                    </span>
                  </span>
                </label>
              ))}
            </div>

            {mode === "restricted" && (
              <div
                style={{
                  marginTop: 16,
                  padding: 14,
                  border: "1px solid var(--border-subtle)",
                  borderRadius: 8,
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                }}
              >
                <div style={{ flex: 1 }}>
                  <strong style={{ fontSize: 13 }}>
                    已选择 {selectedPeople.length} 人
                  </strong>
                  <div
                    style={{
                      color: "var(--text-tertiary)",
                      fontSize: 11,
                      marginTop: 3,
                    }}
                  >
                    {selectedPeople
                      .slice(0, 3)
                      .map((user) => user.name)
                      .join("、") || "尚未选择人员"}
                    {selectedPeople.length > 3
                      ? ` 等 ${selectedPeople.length} 人`
                      : ""}
                  </div>
                </div>
                <button
                  className="btn btn-secondary btn-sm"
                  onClick={() => setShowMemberPicker(true)}
                >
                  选择可访问人员
                </button>
              </div>
            )}

            {pendingUsers.length > 0 && (
              <section style={{ marginTop: 24 }}>
                <h3 style={{ fontSize: 14 }}>待处理申请</h3>
                {pendingUsers.map((user) => (
                  <div
                    key={user.id}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      padding: "10px 0",
                      borderBottom: "1px solid var(--border-subtle)",
                    }}
                  >
                    <span style={{ flex: 1, fontSize: 13 }}>
                      {user.display_name}
                      {user.email ? ` · ${user.email}` : ""}
                    </span>
                    <button
                      className="btn"
                      onClick={() => void resolveRequest(user, "rejected")}
                    >
                      拒绝
                    </button>
                    <button
                      className="btn btn-primary"
                      onClick={() => void resolveRequest(user, "approved")}
                    >
                      允许
                    </button>
                  </div>
                ))}
              </section>
            )}

            <div
              style={{
                position: "sticky",
                bottom: -24,
                background: "var(--bg-primary)",
                padding: "18px 0 24px",
                marginTop: 20,
                textAlign: "right",
              }}
            >
              <button
                className="btn btn-primary"
                disabled={saving || !dirty}
                onClick={() => void save()}
              >
                {saving ? "保存中…" : dirty ? "保存权限" : "已保存"}
              </button>
            </div>
          </>
        ) : (
          <section style={{ marginTop: 18 }}>
            {visitorsLoading ? (
              <p>加载中…</p>
            ) : !visitorData?.items.length ? (
              <p style={{ color: "var(--text-tertiary)", fontSize: 13 }}>
                暂无访问记录
              </p>
            ) : (
              visitorData.items.map((visitor) => (
                <div
                  key={visitor.id}
                  style={{
                    padding: "10px 0",
                    borderBottom: "1px solid var(--border-subtle)",
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1fr) auto",
                    gap: 10,
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div
                      style={{
                        display: "flex",
                        alignItems: "baseline",
                        gap: 8,
                        minWidth: 0,
                      }}
                    >
                      <strong
                        title={visitor.display_name}
                        style={{
                          minWidth: 0,
                          maxWidth: visitor.email ? "42%" : "75%",
                          fontSize: 13,
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                          whiteSpace: "nowrap",
                        }}
                      >
                        {visitor.display_name}
                      </strong>
                      {visitor.email && (
                        <span
                          title={visitor.email}
                          style={{
                            minWidth: 0,
                            color: "var(--text-tertiary)",
                            fontSize: 11,
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          {visitor.email}
                        </span>
                      )}
                      {visitor.visitor_type === "anonymous" && (
                        <span
                          style={{
                            color: "var(--text-tertiary)",
                            fontSize: 10,
                            flexShrink: 0,
                          }}
                        >
                          未登录
                        </span>
                      )}
                    </div>
                    <div
                      style={{
                        color: "var(--text-tertiary)",
                        fontSize: 11,
                        marginTop: 4,
                      }}
                    >
                      首次：{formatTime(visitor.first_viewed_at)} · 最近：
                      {formatTime(visitor.last_viewed_at)}
                    </div>
                  </div>
                  <span
                    style={{ fontSize: 12, color: "var(--text-secondary)" }}
                  >
                    {visitor.view_count} 次
                  </span>
                </div>
              ))
            )}
            {(visitorData?.total || 0) > VISITOR_PAGE_SIZE && (
              <Pagination
                page={visitorPage}
                pageSize={VISITOR_PAGE_SIZE}
                total={visitorData?.total || 0}
                onPageChange={setVisitorPage}
              />
            )}
          </section>
        )}

        <OrgMemberAccessPicker
          open={showMemberPicker}
          agentId={selected.agent_id}
          directoryBaseUrl={`/pages/${selected.id}/directory`}
          membersOnly
          users={selectedPeople}
          departments={[]}
          onClose={() => setShowMemberPicker(false)}
          onSave={async (users) => setSelectedPeople(users)}
        />
      </aside>
    </div>
  );
}
