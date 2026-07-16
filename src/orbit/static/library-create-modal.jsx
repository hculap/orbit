// library-create-modal.jsx — create a new Area or Project in the library.
//
// Phase 2 supports three creation modes:
//   - Manual:  just a name + description; backend scaffolds an empty dir.
//   - ZIP:     upload a .zip; backend extracts under the library root.
//   - Git URL: clone an existing repo by HTTPS/SSH URL. Backend runs
//              `git clone` synchronously; the request can take 60-90 s for
//              large repos, so the submit button stays disabled with a
//              spinner. On 422 the failure detail is surfaced inline.
//
// Endpoints:
//   POST /api/library/{kind}        (Content-Type: application/json)
//     body: { name, description, mode: "manual"|"git_url", git_url? }
//   POST /api/library/{kind}        (multipart/form-data, mode=zip)
//     parts: name, description, mode=zip, file=<File>
//
// Name validation regex (mirrored on the backend):
//   ^[A-Za-z0-9._-][A-Za-z0-9._-]{0,63}$
// — alnum/dot/underscore/dash only. Spaces are rejected because they
// URL-encode to %20 and force shell quoting in every downstream call.

const { useState: _lcmUseState, useEffect: _lcmUseEffect, useMemo: _lcmUseMemo, useCallback: _lcmUseCallback } = React;

// Spaces are no longer allowed — they bleed into URLs as %20 and force
// shell quoting everywhere downstream. Mirror the backend's `_NAME_RE`.
const _LCM_NAME_RE = /^[A-Za-z0-9._-][A-Za-z0-9._-]{0,63}$/;

function _lcmValidateName(raw) {
  const value = (raw || '').trim();
  if (!value) return 'Name is required';
  if (/\s/.test(value)) {
    return 'No spaces — use dash or underscore (e.g. FIFA-WC26-Family-Game)';
  }
  if (!_LCM_NAME_RE.test(value)) {
    return 'Use letters, digits, dot, dash, underscore (max 64)';
  }
  return null;
}

// Extract the last path segment of a git URL (without trailing .git) so we
// can suggest a default name. Examples handled:
//   https://github.com/owner/repo.git    -> "repo"
//   git@github.com:owner/repo.git        -> "repo"
//   https://gitea.example.com/foo/bar/   -> "bar"
function _lcmDeriveNameFromGitUrl(raw) {
  const value = (raw || '').trim();
  if (!value) return '';
  // Strip query/hash fragments first.
  const withoutFragment = value.split('#')[0].split('?')[0];
  // Split on both `/` and `:` (SSH URLs use `host:owner/repo`).
  const parts = withoutFragment.split(/[\/:]/).filter(Boolean);
  if (parts.length === 0) return '';
  let last = parts[parts.length - 1];
  if (last.endsWith('.git')) last = last.slice(0, -4);
  // Stay within the same charset the name validator allows.
  if (!/^[A-Za-z0-9._-][A-Za-z0-9._-]{0,63}$/.test(last)) return '';
  return last;
}

function _lcmIsLikelyGitUrl(raw) {
  const value = (raw || '').trim();
  if (!value) return false;
  if (/^https?:\/\//i.test(value)) return true;
  if (/^git@[^:]+:/.test(value)) return true;
  if (/^ssh:\/\//i.test(value)) return true;
  if (/^git:\/\//i.test(value)) return true;
  return false;
}

function LibraryCreateModal({ kind, onClose, onCreated }) {
  const [mode, setMode] = _lcmUseState('manual'); // 'manual' | 'zip' | 'git_url'
  const [github, setGithub] = _lcmUseState(false);
  const [githubVisibility, setGithubVisibility] = _lcmUseState('private');
  const [name, setName] = _lcmUseState('');
  // Track whether the name field was set explicitly by the user. While
  // false (initial state), we may overwrite it with a suggestion derived
  // from the git URL. As soon as the user types in the name field we stop
  // touching their value.
  const [nameTouched, setNameTouched] = _lcmUseState(false);
  const [description, setDescription] = _lcmUseState('');
  const [file, setFile] = _lcmUseState(null);
  const [gitUrl, setGitUrl] = _lcmUseState('');
  const [submitting, setSubmitting] = _lcmUseState(false);
  const [error, setError] = _lcmUseState(null);
  const toast = (typeof window !== 'undefined' && typeof window.useToast === 'function')
    ? window.useToast()
    : null;

  // Auto-suggest name from the git URL until the user edits the name field.
  _lcmUseEffect(() => {
    if (mode !== 'git_url') return;
    if (nameTouched) return;
    const suggested = _lcmDeriveNameFromGitUrl(gitUrl);
    if (suggested && suggested !== name) setName(suggested);
  }, [mode, gitUrl, nameTouched, name]);

  const kindLabel = kind === 'area' ? 'Area' : 'Project';
  const title = `New ${kindLabel}`;
  const endpoint = _lcmUseMemo(
    () => apiUrl(`/api/library/${encodeURIComponent(kind === 'area' ? 'areas' : 'projects')}`),
    [kind],
  );

  const nameError = _lcmUseMemo(() => _lcmValidateName(name), [name]);
  const fileError = mode === 'zip' && !file ? 'Pick a .zip file' : null;
  const gitUrlError = mode === 'git_url'
    ? (!gitUrl.trim()
        ? 'Git URL is required'
        : (!_lcmIsLikelyGitUrl(gitUrl) ? 'Use http(s)://, ssh://, git://, or git@host:… form' : null))
    : null;

  const submit = _lcmUseCallback(async (e) => {
    if (e && typeof e.preventDefault === 'function') e.preventDefault();
    if (submitting) return;
    setError(null);
    if (nameError) { setError(nameError); return; }
    if (fileError) { setError(fileError); return; }
    if (gitUrlError) { setError(gitUrlError); return; }

    setSubmitting(true);
    try {
      // Backend declares all create-handler params as FastAPI Form(...)/File(...)
      // — every mode posts multipart/form-data. Don't set Content-Type;
      // the browser fills in the multipart boundary automatically.
      const fd = new FormData();
      fd.append('name', name.trim());
      fd.append('description', description.trim());
      fd.append('mode', mode);
      if (mode === 'git_url') {
        fd.append('git_url', gitUrl.trim());
      } else if (mode === 'zip' && file) {
        fd.append('file', file);
      }
      // GitHub creation only makes sense for non-clone modes — git_url
      // already has a remote.
      if (kind === 'project' && mode !== 'git_url' && github) {
        fd.append('github', 'true');
        fd.append('github_visibility', githubVisibility);
      }
      const res = await fetch(endpoint, { method: 'POST', body: fd });
      if (!res.ok) {
        let detail = '';
        try {
          const j = await res.json();
          detail = (j && (j.error || j.detail)) || '';
        } catch (_) {
          try { detail = await res.text(); } catch (__) { /* ignore */ }
        }
        // Surface clone failures with extra context so the user knows whether
        // it was an auth/URL issue vs. server-side error.
        const prefix = (mode === 'git_url' && res.status === 422) ? 'Clone failed: ' : '';
        throw new Error(prefix + (detail ? detail.slice(0, 200) : `http ${res.status}`));
      }
      const data = await res.json().catch(() => ({}));
      // Backend returns 200 with the local project even when the optional
      // GitHub step failed (auth, name collision, etc.) — surface that as a
      // warning toast so the user knows to retry without thinking the whole
      // create failed.
      const ghInfo = data && data.github;
      if (ghInfo && ghInfo.ok === false && ghInfo.error) {
        toast && toast(`${kindLabel} created — GitHub failed: ${ghInfo.error}`, 'warn');
      } else if (ghInfo && ghInfo.ok && ghInfo.url) {
        toast && toast(`${kindLabel} created · ${ghInfo.url}`, 'ok');
      } else {
        toast && toast(`${kindLabel} created`, 'ok');
      }
      window.dispatchEvent(new CustomEvent('library:reload'));
      onCreated && onCreated(data);
      onClose && onClose();
    } catch (err) {
      console.error('library create failed', err);
      setError(err.message || 'Create failed');
    } finally {
      setSubmitting(false);
    }
  }, [endpoint, mode, name, description, file, gitUrl, github, githubVisibility, kind, nameError, fileError, gitUrlError, submitting, onCreated, onClose, kindLabel, toast]);

  const inputStyle = {
    width: '100%', boxSizing: 'border-box',
    background: 'var(--surface-2)', border: '1px solid var(--hairline)',
    borderRadius: 'var(--r-control)', padding: '10px 12px',
    color: 'var(--fg)', fontSize: 'var(--t-sm)', fontFamily: 'inherit', outline: 'none',
  };

  return (
    <Modal open={true} onClose={onClose} title={title} width={520}>
      <form onSubmit={submit} style={{ padding: 18, display: 'flex', flexDirection: 'column', gap: 14 }}>
        {/* mode toggle */}
        <div style={{ display: 'flex', gap: 4, padding: 3, background: 'var(--surface-2)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)', alignSelf: 'flex-start' }}>
          {[
            { k: 'manual',  l: 'Manual' },
            { k: 'zip',     l: 'ZIP' },
            { k: 'git_url', l: 'Git URL' },
          ].map((m) => (
            <button
              type="button"
              key={m.k}
              onClick={() => setMode(m.k)}
              style={{
                padding: '6px 14px', borderRadius: 'var(--r-sm)',
                background: mode === m.k ? 'var(--accent-soft)' : 'transparent',
                color: mode === m.k ? 'var(--accent)' : 'var(--fg-2)',
                border: '1px solid ' + (mode === m.k ? 'var(--accent-line)' : 'transparent'),
                fontSize: 'var(--t-cap)', fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
              }}
            >
              {m.l}
            </button>
          ))}
        </div>

        {mode === 'git_url' && (
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>Git URL</span>
            <Input
              value={gitUrl}
              onChange={setGitUrl}
              placeholder="https://github.com/owner/repo"
              mono
              error={!!(gitUrl && gitUrlError)}
            />
            {gitUrl && gitUrlError && (
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--err)' }}>{gitUrlError}</span>
            )}
          </label>
        )}

        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
            Name
            {mode === 'git_url' && !nameTouched && name && (
              <span style={{ color: 'var(--fg-4)' }}> (suggested from URL)</span>
            )}
          </span>
          <Input
            value={name}
            onChange={(v) => { setNameTouched(true); setName(v); }}
            placeholder={kind === 'area' ? 'e.g. health' : 'e.g. dashboard-ui'}
            autoFocus={mode !== 'git_url'}
            error={!!(name && nameError)}
            mono={false}
          />
          {name && nameError && (
            <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--err)' }}>{nameError}</span>
          )}
        </label>

        {mode === 'zip' && (
          <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>ZIP file</span>
            <input
              type="file"
              accept=".zip,application/zip"
              onChange={(e) => setFile(e.target.files && e.target.files[0] ? e.target.files[0] : null)}
              style={{ ...inputStyle, padding: '8px 10px' }}
            />
            {file && (
              <span className="mono" style={{ fontSize: 'var(--t-xs)', color: 'var(--fg-3)' }}>
                {file.name} · {fmtBytes(file.size)}
              </span>
            )}
          </label>
        )}

        <label style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span style={{ fontSize: 'var(--t-cap)', color: 'var(--fg-3)' }}>
            Description
            {(mode === 'zip' || mode === 'git_url') && (
              <span style={{ color: 'var(--fg-4)' }}> (optional)</span>
            )}
          </span>
          <Textarea
            value={description}
            onChange={setDescription}
            rows={4}
            placeholder="Short description for the index file"
            mono={false}
            style={{ resize: 'vertical', minHeight: 80 }}
            inputStyle={{ lineHeight: 1.5 }}
          />
        </label>

        {/* GitHub repo creation — projects only, not when cloning an existing
            URL (that already has a remote). The checkbox is collapsed by
            default; visibility radio only renders when checked to keep the
            modal compact for the common "just local" case. */}
        {kind === 'project' && mode !== 'git_url' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 'var(--t-sm)', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={github}
                onChange={(e) => setGithub(e.target.checked)}
                style={{ cursor: 'pointer' }}
              />
              <span>Create GitHub repo &amp; push initial commit</span>
            </label>
            {github && (
              <div style={{ display: 'flex', gap: 4, padding: 3, marginLeft: 24, background: 'var(--surface-2)', border: '1px solid var(--hairline)', borderRadius: 'var(--r-control)', alignSelf: 'flex-start' }}>
                {[
                  { v: 'private', l: 'Private' },
                  { v: 'public',  l: 'Public' },
                ].map((opt) => (
                  <button
                    type="button"
                    key={opt.v}
                    onClick={() => setGithubVisibility(opt.v)}
                    style={{
                      padding: '5px 12px', borderRadius: 'var(--r-sm)',
                      background: githubVisibility === opt.v ? 'var(--accent-soft)' : 'transparent',
                      color: githubVisibility === opt.v ? 'var(--accent)' : 'var(--fg-2)',
                      border: '1px solid ' + (githubVisibility === opt.v ? 'var(--accent-line)' : 'transparent'),
                      fontSize: 'var(--t-xs)', fontWeight: 500, cursor: 'pointer', fontFamily: 'inherit',
                    }}
                  >
                    {opt.l}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {error && (
          <StatusBanner variant="err" label={error} inline />
        )}

        {mode === 'git_url' && submitting && (
          <StatusBanner variant="info" label="Cloning repository… this can take 60–90 s for large repos." inline />
        )}

        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 4 }}>
          <Button variant="quiet" onClick={onClose} type="button">Cancel</Button>
          <Button
            variant="primary"
            type="submit"
            icon={submitting ? 'spinner' : 'plus'}
            onClick={submit}
            style={submitting ? { opacity: 0.7, pointerEvents: 'none' } : undefined}
          >
            {submitting
              ? (mode === 'git_url' ? 'Cloning…' : 'Tworzę agenta… (10-30s)')
              : (mode === 'git_url'
                  ? `Clone ${kindLabel.toLowerCase()}`
                  : `Create ${kindLabel.toLowerCase()}`)}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

Object.assign(window, { LibraryCreateModal });
