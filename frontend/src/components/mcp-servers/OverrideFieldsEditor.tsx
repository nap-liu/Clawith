import React, { useEffect, useRef, useState } from 'react';
import type { MCPServerOverridePutPayload } from '../../types/mcpServer';

/**
 * OverrideDraft mirrors the wire format directly:
 *  - `system_prompt_block`: plain string (empty = no override)
 *  - `headers_template`: full record (empty {} = no override)
 *  - `credential_input`: what the user typed in the password box.
 *    Empty string = "don't touch credential" (parent never resets this on
 *    re-render — see save() in OverrideTab which clears it after PUT to
 *    avoid re-submitting on next save).
 */
export interface OverrideDraft {
  system_prompt_block: string;
  headers_template: Record<string, string>;
  credential_input: string;
}

export function emptyDraft(): OverrideDraft {
  return {
    system_prompt_block: '',
    headers_template: {},
    credential_input: '',
  };
}

export function draftToPayload(draft: OverrideDraft): MCPServerOverridePutPayload {
  const payload: MCPServerOverridePutPayload = {
    system_prompt_block: draft.system_prompt_block || null,
    headers_template: draft.headers_template,
  };
  // Only PATCH credential when the user typed something. Empty = "don't touch"
  // (matches BasicTab's behavior on the server's own credential field).
  if (draft.credential_input !== '') {
    payload.credential_template = draft.credential_input;
  }
  return payload;
}

interface Props {
  draft: OverrideDraft;
  onChange: (next: OverrideDraft) => void;
  savedCredentialState?: 'set' | 'unset';
  hide?: { prompt?: boolean; headers?: boolean; credential?: boolean };
  placeholdersHint?: string;
}

const labelStyle: React.CSSProperties = {
  display: 'block',
  fontSize: 11,
  color: 'var(--text-secondary)',
  marginBottom: 4,
  fontWeight: 500,
};

const inputStyle: React.CSSProperties = {
  width: '100%',
  boxSizing: 'border-box',
  padding: '6px 8px',
  fontSize: 12,
  background: 'var(--bg-secondary)',
  border: '1px solid var(--border-subtle)',
  borderRadius: 4,
  color: 'var(--text-primary)',
  fontFamily: 'ui-monospace, monospace',
};

export default function OverrideFieldsEditor({
  draft, onChange, savedCredentialState, hide, placeholdersHint,
}: Props) {
  const show = {
    prompt: !hide?.prompt,
    headers: !hide?.headers,
    credential: !hide?.credential,
  };

  // headers UI state is a stable-ordered array of {key,value} pairs.
  // Using a Record<string,string> directly would unmount the value <input>
  // every time the user edits the key (React reconciles list items by key),
  // which both loses focus mid-typing and creates a stale-closure race when
  // key + value are dispatched in the same tick.
  type Pair = { key: string; value: string };
  const recordToPairs = (r: Record<string, string>): Pair[] =>
    Object.entries(r).map(([key, value]) => ({ key, value }));
  const pairsToRecord = (pairs: Pair[]): Record<string, string> => {
    const out: Record<string, string> = {};
    for (const { key, value } of pairs) {
      if (key) out[key] = value;
    }
    return out;
  };

  const [pairs, setPairs] = useState<Pair[]>(() => recordToPairs(draft.headers_template));
  // Track the last record we *emitted* — when the parent echoes the same value
  // back via draft.headers_template, skip resyncing to avoid clobbering the
  // user's mid-edit key/value state.
  const lastEmittedRef = useRef<string>(JSON.stringify(pairsToRecord(pairs)));
  useEffect(() => {
    const incoming = JSON.stringify(draft.headers_template ?? {});
    if (incoming !== lastEmittedRef.current) {
      setPairs(recordToPairs(draft.headers_template));
      lastEmittedRef.current = incoming;
    }
  }, [draft.headers_template]);

  const commitPairs = (next: Pair[]) => {
    setPairs(next);
    const record = pairsToRecord(next);
    lastEmittedRef.current = JSON.stringify(record);
    onChange({ ...draft, headers_template: record });
  };

  const updateHeader = (idx: number, nextKey: string, nextValue: string) => {
    commitPairs(pairs.map((p, i) => (i === idx ? { key: nextKey, value: nextValue } : p)));
  };

  const addHeader = () => {
    commitPairs([...pairs, { key: '', value: '' }]);
  };

  const removeHeader = (idx: number) => {
    commitPairs(pairs.filter((_, i) => i !== idx));
  };

  const credBadge =
    savedCredentialState === 'set' ? (
      <span style={{
        fontSize: 10, padding: '1px 6px', borderRadius: 3,
        background: 'rgba(34,197,94,0.15)', color: '#22c55e', marginLeft: 6,
      }}>
        已设置
      </span>
    ) : (
      <span style={{
        fontSize: 10, padding: '1px 6px', borderRadius: 3,
        background: 'var(--bg-tertiary)', color: 'var(--text-tertiary)', marginLeft: 6,
      }}>
        未设置
      </span>
    );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {show.prompt && (
        <div>
          <label style={labelStyle}>Prompt 片段 (追加到 platform/tenant 之后)</label>
          <textarea
            value={draft.system_prompt_block}
            onChange={(e) => onChange({ ...draft, system_prompt_block: e.target.value })}
            rows={4}
            placeholder="留空 = 该 override 字段不生效"
            style={{ ...inputStyle, resize: 'vertical' }}
          />
          {placeholdersHint && (
            <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>
              {placeholdersHint}
            </div>
          )}
        </div>
      )}

      {show.headers && (
        <div>
          <label style={labelStyle}>
            HTTP Headers (覆盖 server / tenant 默认值)
          </label>
          {pairs.length === 0 && (
            <div style={{
              fontSize: 11, color: 'var(--text-tertiary)',
              padding: '6px 0',
            }}>
              当前为空(将完全继承上层)。
            </div>
          )}
          {pairs.map((p, idx) => (
            <div key={idx} style={{ display: 'flex', gap: 6, marginBottom: 6 }}>
              <input
                value={p.key}
                onChange={(e) => updateHeader(idx, e.target.value, p.value)}
                placeholder="Header-Name"
                style={{ ...inputStyle, flex: '0 0 35%' }}
              />
              <input
                value={p.value}
                onChange={(e) => updateHeader(idx, p.key, e.target.value)}
                placeholder="value (支持 ${tenant.id} 等占位符)"
                style={{ ...inputStyle, flex: 1 }}
              />
              <button
                type="button"
                onClick={() => removeHeader(idx)}
                style={{
                  padding: '4px 8px', fontSize: 11,
                  background: 'transparent',
                  border: '1px solid var(--border-subtle)',
                  borderRadius: 4, color: 'var(--text-tertiary)',
                  cursor: 'pointer',
                }}
              >
                删除
              </button>
            </div>
          ))}
          <button
            type="button"
            onClick={addHeader}
            style={{
              padding: '4px 10px', fontSize: 11, marginTop: 4,
              background: 'transparent',
              border: '1px dashed var(--border-subtle)',
              borderRadius: 4, color: 'var(--text-secondary)',
              cursor: 'pointer',
            }}
          >
            + 添加 header
          </button>
        </div>
      )}

      {show.credential && (
        <div>
          <label style={labelStyle}>
            Credential (API key / token)
            {savedCredentialState && credBadge}
          </label>
          <input
            type="password"
            value={draft.credential_input}
            onChange={(e) => onChange({ ...draft, credential_input: e.target.value })}
            placeholder={
              savedCredentialState === 'set'
                ? '留空 = 保留已设置的值;填值 = 覆盖'
                : '填值 = 设置 credential;留空 = 不设置(继承上层)'
            }
            autoComplete="new-password"
            style={inputStyle}
          />
          <div style={{ fontSize: 11, color: 'var(--text-tertiary)', marginTop: 4 }}>
            出于安全,服务端不回传已保存的 credential 原文。要改动请重新输入完整值。
          </div>
        </div>
      )}
    </div>
  );
}
