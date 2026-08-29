import { useCallback, useEffect, useState } from 'react';

interface Props {
  apiUrl: string;
  apiKey: string;
}

type Entry = { line: number; level: string; text: string };

type Incident = { source: string; observed_lines: number; error_count: number; warning_count: number; hypothesis: string; errors: Entry[]; warnings: Entry[] };

export function LogCenterPanel({ apiUrl, apiKey }: Props) {
  const [source, setSource] = useState('');
  const [path, setPath] = useState('');
  const [query, setQuery] = useState('');
  const [entries, setEntries] = useState<Entry[]>([]);
  const [incident, setIncident] = useState<Incident | null>(null);
  const [message, setMessage] = useState('');
  const [sources, setSources] = useState<Array<{ name: string; path: string }>>([]);

  const base = apiUrl.replace(/\/$/, '');
  const headers = { Authorization: `Bearer ${apiKey}` };

  const loadSources = useCallback(async () => {
    if (!apiKey) { setMessage('Enter the server API key above first.'); return; }
    try {
      const response = await fetch(`${base}/api/logs/sources`, { headers });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setSources((await response.json()).sources || []);
      setMessage('');
    } catch (error: any) { setMessage(error?.message || 'Unable to load log sources'); }
  }, [apiKey, base]);

  useEffect(() => { void loadSources(); }, [loadSources]);

  const register = async () => {
    if (!source.trim() || !path.trim()) return;
    try {
      const response = await fetch(`${base}/api/logs/sources`, { method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify({ name: source.trim(), relative_path: path.trim() }) });
      if (!response.ok) throw new Error((await response.json()).detail || `HTTP ${response.status}`);
      setPath('');
      await loadSources();
    } catch (error: any) { setMessage(error?.message || 'Unable to register source'); }
  };

  const search = async () => {
    if (!source) return;
    try {
      const params = new URLSearchParams({ contains: query, limit: '200' });
      const response = await fetch(`${base}/api/logs/${encodeURIComponent(source)}/search?${params}`, { headers });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setEntries((await response.json()).entries || []);
      setMessage('');
    } catch (error: any) { setMessage(error?.message || 'Unable to search logs'); }
  };

  const inspect = async () => {
    if (!source) return;
    try {
      const response = await fetch(`${base}/api/logs/${encodeURIComponent(source)}/incident`, { headers });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setIncident(await response.json());
    } catch (error: any) { setMessage(error?.message || 'Unable to analyze incident'); }
  };

  return (
    <div className="rounded-xl p-5 mt-4" style={{ background: 'var(--color-surface)', border: '1px solid var(--color-border)' }}>
      <h3 className="text-sm font-semibold mb-1" style={{ color: 'var(--color-text)' }}>Log Center</h3>
      <p className="text-xs mb-3" style={{ color: 'var(--color-text-tertiary)' }}>Read-only search, redaction, tail-ready monitoring, and incident summaries inside the configured log root.</p>
      <div className="flex flex-wrap gap-2 mb-3">
        <input value={source} onChange={e => setSource(e.target.value)} placeholder="source name" className="text-xs px-2 py-1 rounded" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        <input value={path} onChange={e => setPath(e.target.value)} placeholder="relative path, e.g. service.log" className="text-xs px-2 py-1 rounded w-48" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        <button onClick={() => void register()} className="text-xs px-2 py-1 rounded cursor-pointer" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}>Register</button>
        <button onClick={() => void loadSources()} className="text-xs px-2 py-1 rounded cursor-pointer" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}>Refresh</button>
      </div>
      <div className="text-xs mb-3" style={{ color: 'var(--color-text-tertiary)' }}>Sources: {sources.map(item => `${item.name} (${item.path})`).join(', ') || 'none'}</div>
      <div className="flex flex-wrap gap-2 mb-3">
        <input value={query} onChange={e => setQuery(e.target.value)} placeholder="search text (secret values are redacted)" className="text-xs px-2 py-1 rounded w-64" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        <button onClick={() => void search()} className="text-xs px-2 py-1 rounded cursor-pointer" style={{ background: 'var(--color-accent)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}>Search</button>
        <button onClick={() => void inspect()} className="text-xs px-2 py-1 rounded cursor-pointer" style={{ background: 'var(--color-bg-tertiary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}>Analyze incident</button>
      </div>
      {message && <div className="text-xs mb-2" style={{ color: 'var(--color-error)' }}>{message}</div>}
      {entries.length > 0 && <pre className="text-[11px] whitespace-pre-wrap max-h-48 overflow-auto p-2 rounded" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text-secondary)' }}>{entries.map(item => `${item.line} [${item.level}] ${item.text}`).join('\n')}</pre>}
      {incident && <div className="text-xs mt-3" style={{ color: 'var(--color-text-secondary)' }}>Observed {incident.observed_lines} lines; errors {incident.error_count}, warnings {incident.warning_count}. {incident.hypothesis}</div>}
    </div>
  );
}
