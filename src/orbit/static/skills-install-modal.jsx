// skills-install-modal.jsx — install a Skill into the registry.
//
// Three modes: GitHub (URL or owner/repo[@ref] shorthand auto-detected), ZIP
// upload, and "Create via AI" (textarea description → backend spawns claude
// to scaffold SKILL.md and registers it).
//
// Endpoints:
//   POST /api/skills/install
//     - JSON for github / custom modes:
//         { source: "github"|"shorthand"|"custom",
//           url? | repo? | description? | name?+skill_md?, ref?, name_override?,
//           enable_for: ["global"|"<kind>:<lib_id>", ...] }
//     - multipart/form-data for ZIP:
//         file=<File>, source="zip",
//         enable_for=<JSON-stringified array>, name_hint?

const { useState: _simUseState, useEffect: _simUseEffect, useMemo: _simUseMemo, useCallback: _simUseCallback } = React;

// Accepts either full GitHub URL or owner/repo[@ref] shorthand.
const _SIM_GH_INPUT_RE = /^(?:https?:\/\/github\.com\/)?[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+(?:\.git)?(?:@[A-Za-z0-9._\-/]+)?\/?$/i;
const _SIM_REQUEST_TIMEOUT_MS = 30000;
const _SIM_AI_TIMEOUT_MS = 180000;
const _SIM_DESCRIPTION_MAX = 4000;

function _simInputStyle() {
  return {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
  };
}

function _simNormalizeGhInput(raw) {
  // Accepts full URL or owner/repo[@ref]. Returns { url, ref, isShorthand }.
  const value = (raw || '').trim();
  if (!_SIM_GH_INPUT_RE.test(value)) return null;
  const isShorthand = !/^https?:\/\//i.test(value);
  let cleaned = value.replace(/^https?:\/\/github\.com\//i, '');
  cleaned = cleaned.replace(/\.git$/i, '').replace(/\/$/, '');
  let ref = '';
  const atIdx = cleaned.indexOf('@');
  if (atIdx >= 0) {
    ref = cleaned.slice(atIdx + 1);
    cleaned = cleaned.slice(0, atIdx);
  }
  return { url: `https://github.com/${cleaned}`, ref, isShorthand, repo: cleaned };
}

function _simFetchWithTimeout(url, options, timeoutMs) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  return fetch(url, { ...options, signal: controller.signal })
    .finally(() => clearTimeout(timer));
}

async function _simReadError(response) {
  let detail = `http ${response.status}`;
  try {
    const data = await response.json();
    if (data && (data.error || data.detail)) detail = String(data.error || data.detail).slice(0, 280);
  } catch (_) { /* ignore */ }
  return detail;
}

// Build the list of agents the user can opt-in for. Pulled from useHubData()
// (areas + projects + resources) plus the synthetic "global" entry. We don't
// fetch a separate /api/library/all endpoint — the in-memory list already
// covers everything visible elsewhere in the UI.
function _simBuildAgentOptions(hubData) {
  const opts = [{ value: 'global', label: 'Global', icon: '🤖', sub: 'default ~/' }];
  const push = (item, kind, fallbackIcon) => {
    const libId = item.lib_id || item.name;
    if (!libId) return;
    opts.push({
      value: `${kind}:${libId}`,
      label: item.label || item.name || libId,
      icon: item.icon || fallbackIcon,
      sub: kind,
    });
  };
  ((hubData && hubData.areas) || []).forEach((a) => push(a, 'areas', '📂'));
  ((hubData && hubData.projects) || []).forEach((p) => push(p, 'projects', '📦'));
  ((hubData && hubData.resources) || []).forEach((r) => push(r, 'resources', '📚'));
  return opts;
}

// ─────────────────────────────────────────────────────────────
// Tab strip — picks the install source.
// ─────────────────────────────────────────────────────────────
function _SimTabStrip({ mode, onChange }) {
  const tabs = [
    { k: 'github', l: 'GitHub' },
    { k: 'zip',    l: 'ZIP' },
    { k: 'ai',     l: 'Create via AI' },
  ];
  return (
    <div style={{
      display: 'flex', gap: 4, padding: 3, flexWrap: 'wrap',
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', alignSelf: 'flex-start',
    }}>
      {tabs.map((t) => (
        <button
          type="button"
          key={t.k}
          onClick={() => onChange(t.k)}
          style={{
            padding: '6px 14px', borderRadius: 'var(--r-sm)',
            background: mode === t.k ? 'var(--accent-soft)' : 'transparent',
            color: mode === t.k ? 'var(--accent)' : 'var(--fg-2)',
            border: '1px solid ' + (mode === t.k ? 'var(--accent-line)' : 'transparent'),
            fontSize: 'var(--t-cap)', fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
          }}
        >
          {t.l}
        </button>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Drop-zone — used by the ZIP tab.
// ─────────────────────────────────────────────────────────────
function _SimDropZone({ file, onFile }) {
  const [drag, setDrag] = _simUseState(false);
  const inputRef = React.useRef(null);
  const onDrop = (e) => {
    e.preventDefault(); setDrag(false);
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) onFile(f);
  };
  return (
    <div>
      <div
        onClick={() => inputRef.current && inputRef.current.click()}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={onDrop}
        style={{
          padding: '22px 18px', borderRadius: 'var(--r-md)', cursor: 'pointer',
          textAlign: 'center', userSelect: 'none',
          background: drag ? 'var(--accent-soft)' : 'var(--surface-2)',
          border: '1px dashed ' + (drag ? 'var(--accent-line)' : 'var(--hairline-strong)'),
          color: drag ? 'var(--accent)' : 'var(--fg-2)',
        }}
      >
        <div style={{ fontSize: 'var(--t-sm)', fontWeight: 500 }}>
          {file ? 'Replace file' : 'Drop a .zip here or click to browse'}
        </div>
        <div className="mono" style={{ marginTop: 6, fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
          Archive must contain SKILL.md at root or under a single subdir.
        </div>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept=".zip,application/zip"
        onChange={(e) => {
          const f = e.target.files && e.target.files[0];
          if (f) onFile(f);
        }}
        style={{ display: 'none' }}
      />
      {file && (
        <div className="mono" style={{
          marginTop: 8, padding: '8px 10px', borderRadius: 'var(--r-sm)',
          background: 'var(--surface-2)', border: '1px solid var(--hairline)',
          fontSize: 'var(--t-xs)', color: 'var(--fg-2)',
          display: 'flex', alignItems: 'center', gap: 8,
        }}>
          <Icon name="archive" size={14} />
          <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {file.name}
          </span>
          <span style={{ color: 'var(--fg-4)' }}>{fmtBytes(file.size)}</span>
        </div>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// AgentMultiselect — list of agents with checkboxes (defaults to "global").
// ─────────────────────────────────────────────────────────────
function _SimAgentMultiselect({ options, selected, onToggle }) {
  return (
    <div style={{
      maxHeight: 200, overflowY: 'auto',
      background: 'var(--surface-2)', border: '1px solid var(--hairline)',
      borderRadius: 'var(--r-control)', padding: 4,
    }} className="scroll-hide">
      {options.map((opt) => {
        const checked = selected.has(opt.value);
        return (
          <label key={opt.value} style={{
            display: 'flex', alignItems: 'center', gap: 10,
            padding: '8px 10px', borderRadius: 'var(--r-sm)', cursor: 'pointer',
            background: checked ? 'var(--accent-soft)' : 'transparent',
          }}>
            <input
              type="checkbox"
              checked={checked}
              onChange={() => onToggle(opt.value)}
              style={{ accentColor: 'var(--accent)' }}
            />
            <span style={{ fontSize: 'var(--t-h3)', lineHeight: 1, width: 22, textAlign: 'center' }}>{opt.icon}</span>
            <span style={{ flex: 1, minWidth: 0, fontSize: 'var(--t-sm)', fontWeight: 500, color: 'var(--fg)' }}>
              {opt.label}
            </span>
            <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)', textTransform: 'uppercase' }}>
              {opt.sub}
            </span>
          </label>
        );
      })}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────
function SkillsInstallModal({ onClose, onInstalled }) {
  const hubData = (typeof window.useHubData === 'function') ? window.useHubData() : null;
  const router = (typeof window.useRouter === 'function') ? window.useRouter() : null;
  const toast = (typeof window.useToast === 'function') ? window.useToast() : null;

  const [mode, setMode] = _simUseState('github');
  const [ghInput, setGhInput] = _simUseState('');
  const [ghRef, setGhRef] = _simUseState('');
  const [file, setFile] = _simUseState(null);
  const [description, setDescription] = _simUseState('');
  const [nameOverride, setNameOverride] = _simUseState('');
  const [enabledFor, setEnabledFor] = _simUseState(() => new Set(['global']));
  const [submitting, setSubmitting] = _simUseState(false);
  const [error, setError] = _simUseState(null);

  const agentOptions = _simUseMemo(() => _simBuildAgentOptions(hubData), [hubData]);

  const ghParsed = _simUseMemo(() => {
    if (mode !== 'github') return null;
    return _simNormalizeGhInput(ghInput);
  }, [mode, ghInput]);

  const ghError = _simUseMemo(() => {
    if (mode !== 'github') return null;
    if (!ghInput.trim()) return 'GitHub URL or owner/repo is required';
    if (!ghParsed) return 'Use https://github.com/<owner>/<repo> or owner/repo[@ref]';
    return null;
  }, [mode, ghInput, ghParsed]);

  const fileError = mode === 'zip' && !file ? 'Pick a .zip file' : null;

  const descriptionError = _simUseMemo(() => {
    if (mode !== 'ai') return null;
    const v = description.trim();
    if (!v) return 'Description is required';
    if (v.length > _SIM_DESCRIPTION_MAX) return `Description too long (≤${_SIM_DESCRIPTION_MAX} chars)`;
    return null;
  }, [mode, description]);

  const toggle = _simUseCallback((value) => {
    setEnabledFor((prev) => {
      const next = new Set(prev);
      if (next.has(value)) next.delete(value); else next.add(value);
      return next;
    });
  }, []);

  const submitting_label = _simUseMemo(() => {
    if (mode === 'github') return 'Cloning repo…';
    if (mode === 'zip') return 'Extracting ZIP…';
    if (mode === 'ai') return 'Generating skill via Claude…';
    return 'Installing…';
  }, [mode]);

  const submit = _simUseCallback(async (e) => {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    if (submitting) return;
    setError(null);

    if (ghError) { setError(ghError); return; }
    if (fileError) { setError(fileError); return; }
    if (descriptionError) { setError(descriptionError); return; }
    if (enabledFor.size === 0) {
      setError('Pick at least one agent (Global is the safe default).');
      return;
    }

    setSubmitting(true);
    try {
      const enable_for = Array.from(enabledFor);
      let response;
      const timeoutMs = mode === 'ai' ? _SIM_AI_TIMEOUT_MS : _SIM_REQUEST_TIMEOUT_MS;

      if (mode === 'zip') {
        const fd = new FormData();
        fd.append('source', 'zip');
        fd.append('file', file);
        fd.append('enable_for', JSON.stringify(enable_for));
        if (nameOverride.trim()) fd.append('name_hint', nameOverride.trim());
        response = await _simFetchWithTimeout(
          apiUrl('/api/skills/install'),
          { method: 'POST', body: fd },
          timeoutMs,
        );
      } else {
        const body = { enable_for };
        if (mode === 'github') {
          // Pick shorthand vs URL flow per the input format detected earlier.
          if (ghParsed && ghParsed.isShorthand) {
            body.source = 'shorthand';
            body.repo = ghParsed.repo;
            const ref = (ghRef.trim() || ghParsed.ref).trim();
            if (ref) body.ref = ref;
          } else {
            body.source = 'github';
            body.url = ghParsed ? ghParsed.url : ghInput.trim();
            const ref = (ghRef.trim() || (ghParsed && ghParsed.ref) || '').trim();
            if (ref) body.ref = ref;
          }
        } else if (mode === 'ai') {
          body.source = 'custom';
          body.description = description.trim();
        }
        if (mode !== 'ai' && nameOverride.trim()) {
          body.name_override = nameOverride.trim();
        }
        response = await _simFetchWithTimeout(
          apiUrl('/api/skills/install'),
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          },
          timeoutMs,
        );
      }
      if (!response.ok) throw new Error(await _simReadError(response));
      const data = await response.json().catch(() => ({}));
      const installedName = data && (
        data.name
        || (data.skill && data.skill.name)
        || (Array.isArray(data.installed) && data.installed[0] && data.installed[0].name)
      );
      try { window.dispatchEvent(new CustomEvent('skills:reload')); } catch (_) { /* ignore */ }
      toast && toast('Skill installed', 'ok');
      if (typeof onInstalled === 'function') onInstalled(data);
      if (router && installedName && typeof router.push === 'function') {
        router.push(`/skills/${encodeURIComponent(installedName)}`);
      }
      if (onClose) onClose();
    } catch (err) {
      const msg = (err && err.name === 'AbortError')
        ? `Request timed out (${(mode === 'ai' ? _SIM_AI_TIMEOUT_MS : _SIM_REQUEST_TIMEOUT_MS) / 1000}s)`
        : (err.message || 'Install failed');
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }, [submitting, mode, ghError, ghParsed, ghInput, ghRef, fileError, descriptionError, description, file, enabledFor, nameOverride, onClose, onInstalled, router, toast]);

  const inputStyle = _simInputStyle();
  const disableSubmit = (
    submitting
    || !!ghError
    || !!fileError
    || !!descriptionError
    || enabledFor.size === 0
  );

  return (
    <Modal open={true} onClose={onClose} title="Install skill" width={560}>
      <form onSubmit={submit} style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        <_SimTabStrip mode={mode} onChange={(m) => { setMode(m); setError(null); }} />

        {mode === 'github' && (
          <>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
                GitHub URL <span style={{ color: 'var(--fg-4)' }}>or</span> owner/repo[@ref]
              </span>
              <Input
                value={ghInput}
                onChange={setGhInput}
                placeholder="https://github.com/owner/repo  or  obra/superpowers@v5.1.0"
                mono
                autoFocus
                error={!!(ghInput && ghError)}
              />
              {ghInput && ghError && (
                <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--err)' }}>{ghError}</span>
              )}
              {ghParsed && ghParsed.isShorthand && (
                <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-4)' }}>
                  → {ghParsed.url}{ghParsed.ref ? ` @ ${ghParsed.ref}` : ''}
                </span>
              )}
            </label>
            <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Ref (optional)</span>
              <Input
                value={ghRef}
                onChange={setGhRef}
                placeholder={(ghParsed && ghParsed.ref) || 'main'}
                mono
              />
            </label>
          </>
        )}

        {mode === 'zip' && (
          <_SimDropZone file={file} onFile={setFile} />
        )}

        {mode === 'ai' && (
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
              Describe the skill you want
            </span>
            <Textarea
              value={description}
              onChange={setDescription}
              placeholder={'np. "Skill który zamienia notatki głosowe Telegram na markdown w ~/Inbox/voice/<data>.md, używa whisper API. Triggers: zapisz nagranie, transkrybuj voice."'}
              rows={9}
              mono={false}
              autoFocus
              error={!!(description && descriptionError)}
              style={{ minHeight: 140 }}
              inputStyle={{ fontSize: 'var(--t-sm)', lineHeight: 1.5 }}
            />
            <div style={{
              display: 'flex', justifyContent: 'space-between', alignItems: 'center',
              fontSize: 'var(--t-xs)', color: 'var(--fg-4)',
            }}>
              <span className="mono">
                Backend zatrudnia Global agent do scaffoldu SKILL.md (≤{Math.round(_SIM_AI_TIMEOUT_MS / 1000)}s).
              </span>
              <span className="mono" style={{
                color: description.length > _SIM_DESCRIPTION_MAX ? 'var(--err)' : 'var(--fg-4)',
              }}>
                {description.length}/{_SIM_DESCRIPTION_MAX}
              </span>
            </div>
            {description && descriptionError && (
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--err)' }}>{descriptionError}</span>
            )}
          </label>
        )}

        {mode !== 'ai' && (
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
              Name override <span style={{ color: 'var(--fg-4)' }}>(optional)</span>
            </span>
            <Input
              value={nameOverride}
              onChange={setNameOverride}
              placeholder="Leave blank to derive from source"
            />
          </label>
        )}

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Enable for which agents?</span>
          <_SimAgentMultiselect
            options={agentOptions}
            selected={enabledFor}
            onToggle={toggle}
          />
          <span className="mono" style={{ fontSize: 'var(--t-2xs)', color: 'var(--fg-4)' }}>
            Global is checked by default — every spawn of the Global agent will see this skill.
          </span>
        </div>

        {error && (
          <StatusBanner variant="err" label={error} inline />
        )}

        {submitting && (
          <StatusBanner variant="info" label={submitting_label} inline />
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
          <Button variant="quiet" onClick={onClose} type="button">Cancel</Button>
          <Button
            variant="primary"
            type="submit"
            icon={submitting ? 'spinner' : (mode === 'ai' ? 'sparkle' : 'download')}
            onClick={submit}
            style={disableSubmit ? { opacity: 0.5, pointerEvents: 'none' } : undefined}
          >
            {submitting ? submitting_label : (mode === 'ai' ? 'Generate skill' : 'Install skill')}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

Object.assign(window, { SkillsInstallModal });
