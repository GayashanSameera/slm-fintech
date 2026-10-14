// Asks for the client's API key (and, in "advanced", the API URL).
// The key is kept in sessionStorage by App: this browser tab only, gone when it closes.

import { useState } from "react";
import type { Settings } from "../api";

interface Props {
  initial: Settings;
  lockedUrl: boolean;           // true when config.json fixes the API URL (the hosted console)
  error?: string;
  onSave: (s: Settings) => void;
  onCancel?: () => void;
}

export function KeyDialog({ initial, lockedUrl, error, onSave, onCancel }: Props) {
  const [apiUrl, setApiUrl] = useState(initial.apiUrl);
  const [apiKey, setApiKey] = useState(initial.apiKey);
  const [advanced, setAdvanced] = useState(!lockedUrl && !initial.apiUrl);

  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-labelledby="key-title">
      <form
        className="dialog"
        onSubmit={(e) => { e.preventDefault(); onSave({ apiUrl: apiUrl.trim(), apiKey: apiKey.trim() }); }}
      >
        <h2 id="key-title">Connect with your API key</h2>
        <p className="muted">
          Paste the API key you received. It stays in this browser tab only and is sent only to the
          extraction API. Closing the tab forgets it.
        </p>
        <label className="field">
          API key
          <input type="password" autoComplete="off" spellCheck={false} placeholder="slm_live_..." value={apiKey}
                 onChange={(e) => setApiKey(e.target.value)} autoFocus required />
        </label>
        {advanced ? (
          <label className="field">
            API URL
            <input className="mono" placeholder="https://xxxx.execute-api.us-east-1.amazonaws.com/prod" value={apiUrl}
                   onChange={(e) => setApiUrl(e.target.value)} disabled={lockedUrl} required />
          </label>
        ) : (
          <button type="button" className="link" onClick={() => setAdvanced(true)}>Show API URL</button>
        )}
        {error && <div className="notice bad">{error}</div>}
        <div className="row end">
          {onCancel && <button type="button" onClick={onCancel}>Cancel</button>}
          <button type="submit" className="primary">Connect</button>
        </div>
      </form>
    </div>
  );
}
