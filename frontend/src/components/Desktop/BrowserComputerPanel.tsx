import { useEffect, useMemo, useState } from 'react';
import { Monitor, Play, Pause, Square, RefreshCw, ShieldAlert } from 'lucide-react';

type Session = { session_id: string; status: string; paused: boolean; captcha_pending: boolean; url: string };
type EventItem = { event_type: string; payload: Record<string, unknown>; created_at: string };

export function BrowserComputerPanel({ apiUrl, apiKey }: { apiUrl: string; apiKey: string }) {
  const [session, setSession] = useState<Session | null>(null);
  const [url, setUrl] = useState('https://example.com');
  const [selector, setSelector] = useState('');
  const [value, setValue] = useState('');
  const [events, setEvents] = useState<EventItem[]>([]);
  const [imageUrl, setImageUrl] = useState('');
  const [message, setMessage] = useState('');
  const [approvalId, setApprovalId] = useState('');
  const [mouseX, setMouseX] = useState('0');
  const [mouseY, setMouseY] = useState('0');
  const [manualKey, setManualKey] = useState('Enter');
  const base = useMemo(() => apiUrl.replace(/\/$/, ''), [apiUrl]);
  const headers = useMemo(() => ({ Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' }), [apiKey]);

  const call = async (path: string, init?: RequestInit) => {
    const response = await fetch(`${base}${path}`, { ...init, headers: { ...headers, ...(init?.headers || {}) } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response;
  };

  const refresh = async (id = session?.session_id) => {
    if (!id) return;
    try {
      const state = await (await call(`/api/browser/computers/sessions/${id}`)).json();
      setSession(state.session);
      const eventData = await (await call(`/api/browser/computers/sessions/${id}/events?limit=50`)).json();
      setEvents(eventData.events || []);
      const shot = await call(`/api/browser/computers/sessions/${id}/screenshot`);
      const blob = await shot.blob();
      setImageUrl(URL.createObjectURL(blob));
    } catch (error: any) {
      setMessage(error?.message || 'Browser session unavailable');
    }
  };

  useEffect(() => () => { if (imageUrl) URL.revokeObjectURL(imageUrl); }, [imageUrl]);

  const create = async () => {
    try {
      const response = await call('/api/browser/computers/sessions', { method: 'POST', body: JSON.stringify({ headless: true }) });
      const data = await response.json();
      setSession(data.session);
      setMessage('Isolated browser computer started');
      void refresh(data.session.session_id);
    } catch (error: any) { setMessage(error?.message || 'Unable to start browser'); }
  };

  const navigate = async () => {
    if (!session) return;
    try {
      const response = await call(`/api/browser/computers/sessions/${session.session_id}/navigate`, { method: 'POST', body: JSON.stringify({ url }) });
      const data = await response.json();
      setSession(data.session);
      setMessage(data.result.captcha_detected ? 'CAPTCHA detected — manual takeover required' : 'Navigated');
      void refresh();
    } catch (error: any) { setMessage(error?.message || 'Navigation failed'); }
  };

  const action = async (actionName: string, actionValue = '') => {
    if (!session) return;
    try {
      const response = await call(`/api/browser/computers/sessions/${session.session_id}/actions`, { method: 'POST', body: JSON.stringify({ action: actionName, selector, value: actionValue }) });
      const data = await response.json();
      if (data.result.approval_required) {
        setApprovalId(data.result.approval_id);
        setMessage('Approval required before this side effect');
      } else {
        setMessage(data.result.success ? 'Action completed' : (data.result.error || 'Action blocked'));
      }
      void refresh();
    } catch (error: any) { setMessage(error?.message || 'Action failed'); }
  };

  const approve = async () => {
    if (!session || !approvalId) return;
    try {
      const response = await call(`/api/browser/computers/sessions/${session.session_id}/approve`, { method: 'POST', body: JSON.stringify({ approval_id: approvalId }) });
      const data = await response.json();
      setMessage(data.approved ? 'Approved once — repeat the action to execute it' : 'Approval expired or invalid');
      if (data.approved) setApprovalId('');
      void refresh();
    } catch (error: any) { setMessage(error?.message || 'Approval failed'); }
  };

  const manualInput = async (actionName: string) => {
    if (!session) return;
    try {
      const response = await call(`/api/browser/computers/sessions/${session.session_id}/manual-input`, { method: 'POST', body: JSON.stringify({ action: actionName, x: Number(mouseX), y: Number(mouseY), key: manualKey }) });
      const data = await response.json();
      setMessage(data.result.success ? 'Manual input sent to server browser' : (data.result.error || 'Manual input blocked'));
      void refresh();
    } catch (error: any) { setMessage(error?.message || 'Manual input failed'); }
  };

  const simplePost = async (path: string, body?: Record<string, unknown>) => {
    if (!session) return;
    try { await call(`/api/browser/computers/sessions/${session.session_id}${path}`, { method: 'POST', body: JSON.stringify(body || {}) }); void refresh(); }
    catch (error: any) { setMessage(error?.message || 'Request failed'); }
  };

  return (
    <div className="rounded-xl p-4 mt-3" style={{ background: 'var(--color-bg-secondary)', border: '1px solid var(--color-border)' }}>
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <Monitor size={16} style={{ color: 'var(--color-accent)' }} />
          <div><div className="text-sm font-semibold" style={{ color: 'var(--color-text)' }}>Server Browser Computer</div><div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>Isolated Chrome session with approval gates</div></div>
        </div>
        {!session ? <button onClick={() => void create()} className="text-xs px-3 py-1.5 rounded-lg" style={{ background: 'var(--color-accent)', color: 'white' }}><Play size={12} className="inline mr-1" />Start</button> : <span className="text-xs" style={{ color: session.captcha_pending ? 'var(--color-warning)' : 'var(--color-success)' }}>{session.status}</span>}
      </div>
      {session && <>
        <div className="flex gap-2 mb-2"><input value={url} onChange={e => setUrl(e.target.value)} className="flex-1 text-xs px-2 py-1.5 rounded" placeholder="https://..." style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><button onClick={() => void navigate()} className="text-xs px-2 rounded" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}>Go</button></div>
        <div className="flex gap-2 mb-2"><input value={selector} onChange={e => setSelector(e.target.value)} className="flex-1 text-xs px-2 py-1.5 rounded" placeholder="CSS selector" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><input value={value} onChange={e => setValue(e.target.value)} className="flex-1 text-xs px-2 py-1.5 rounded" placeholder="value / scroll pixels" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></div>
        <div className="flex flex-wrap gap-2 mb-3"><button onClick={() => void action('click')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}>Click</button><button onClick={() => void action('fill')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}>Fill</button><button onClick={() => void action('scroll', value || '800')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}>Scroll</button><button onClick={() => void refresh()} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}><RefreshCw size={12} className="inline mr-1" />Refresh</button><button onClick={() => void simplePost('/pause')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-border)', color: 'var(--color-text)' }}><Pause size={12} className="inline mr-1" />Pause</button><button onClick={() => void simplePost('/stop')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-error)', color: 'var(--color-error)' }}><Square size={12} className="inline mr-1" />Stop</button></div>
        {approvalId && <div className="flex items-center justify-between gap-2 p-2 rounded mb-3 text-xs" style={{ background: 'rgba(220,38,38,0.12)', color: 'var(--color-error)' }}><span>Approval required for the requested side effect.</span><button onClick={() => void approve()} className="px-2 py-1 rounded" style={{ border: '1px solid currentColor' }}>Approve once</button></div>}
        {session.captcha_pending && <div className="flex flex-wrap items-center gap-2 mb-3"><input value={mouseX} onChange={e => setMouseX(e.target.value)} className="w-16 text-xs px-2 py-1 rounded" placeholder="x" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><input value={mouseY} onChange={e => setMouseY(e.target.value)} className="w-16 text-xs px-2 py-1 rounded" placeholder="y" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><button onClick={() => void manualInput('mouse_click')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-warning)', color: 'var(--color-warning)' }}>Manual click</button><input value={manualKey} onChange={e => setManualKey(e.target.value)} className="w-20 text-xs px-2 py-1 rounded" placeholder="Enter" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><button onClick={() => void manualInput('key_press')} className="text-xs px-2 py-1 rounded" style={{ border: '1px solid var(--color-warning)', color: 'var(--color-warning)' }}>Key</button></div>}
        {session.captcha_pending && <div className="flex items-center justify-between gap-2 p-2 rounded mb-3 text-xs" style={{ background: 'rgba(245,158,11,0.12)', color: 'var(--color-warning)' }}><span><ShieldAlert size={14} className="inline mr-1" />CAPTCHA: take over the server browser manually, then resume.</span><button onClick={() => void simplePost('/captcha/resume')} className="px-2 py-1 rounded" style={{ border: '1px solid currentColor' }}>Resume</button></div>}
        {imageUrl && <img src={imageUrl} alt="Live server browser viewport" className="w-full rounded-lg mb-3" style={{ maxHeight: 420, objectFit: 'contain', background: '#111' }} />}
        {message && <div className="text-xs mb-2" style={{ color: 'var(--color-text-secondary)' }}>{message}</div>}
        <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>Current URL: {session.url || 'about:blank'}</div>
        <div className="mt-2 max-h-32 overflow-y-auto space-y-1">{events.slice().reverse().map((event, index) => <div key={`${event.created_at}-${index}`} className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>{event.event_type} · {event.created_at}</div>)}</div>
      </>}
    </div>
  );
}
