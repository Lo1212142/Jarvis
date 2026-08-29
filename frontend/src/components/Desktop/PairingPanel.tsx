import { useCallback, useEffect, useState } from 'react';

interface PairingPanelProps {
  apiUrl: string;
  apiKey: string;
}

type Pairing = { pairing_id: string; server_url: string; fingerprint: string; expires_at: number; scopes: string[]; device_name_hint: string };
type Device = { device_id: string; name: string; scopes: string[]; created_at: number; last_seen_at: number; revoked: boolean };

export function PairingPanel({ apiUrl, apiKey }: PairingPanelProps) {
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [devices, setDevices] = useState<Device[]>([]);
  const [name, setName] = useState('Boss phone');
  const [includeApprovals, setIncludeApprovals] = useState(false);
  const [includeLogs, setIncludeLogs] = useState(false);
  const [qrImageUrl, setQrImageUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const base = apiUrl.replace(/\/$/, '');
  const headers = { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' };

  const loadDevices = useCallback(async () => {
    if (!apiKey) return;
    try {
      const response = await fetch(`${base}/api/pairing/devices`, { headers });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setDevices((await response.json()).devices || []);
      setError('');
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Unable to load devices');
    }
  }, [apiKey, base]);

  useEffect(() => { void loadDevices(); }, [loadDevices]);

  async function createPairing() {
    setBusy(true); setError('');
    try {
      const scopes = ['chat', 'events', 'read', 'camera', 'audio', 'jobs', 'reminders', 'browser', 'media', ...(includeApprovals ? ['approvals'] : []), ...(includeLogs ? ['logs'] : [])];
      const response = await fetch(`${base}/api/pairing/create`, {
        method: 'POST', headers,
        body: JSON.stringify({
          server_url: base,
          device_name_hint: name.trim(),
          // Approval is deliberately opt-in: it permits this paired phone to
          // approve/reject existing server-side requests, never create one.
          ...((includeApprovals || includeLogs) ? { scopes } : {}),
        }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setPairing(await response.json());
      await loadDevices();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Unable to create pairing');
    } finally { setBusy(false); }
  }

  useEffect(() => {
    let active = true;
    let objectUrl = '';
    setQrImageUrl('');
    if (!pairing || !apiKey || pairing.expires_at * 1000 <= Date.now()) return undefined;
    void (async () => {
      try {
        const response = await fetch(
          `${base}/api/pairing/${encodeURIComponent(pairing.pairing_id)}/qr.png`,
          { headers: { Authorization: `Bearer ${apiKey}` } },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        objectUrl = URL.createObjectURL(await response.blob());
        if (active) setQrImageUrl(objectUrl);
      } catch (exc) {
        if (active) setError(exc instanceof Error ? exc.message : 'Unable to load QR image');
      }
    })();
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [apiKey, base, pairing]);

  async function revoke(deviceId: string) {
    if (!window.confirm('Revoke this device?')) return;
    setBusy(true); setError('');
    try {
      const response = await fetch(`${base}/api/pairing/devices/${encodeURIComponent(deviceId)}`, { method: 'DELETE', headers });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await loadDevices();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : 'Unable to revoke device');
    } finally { setBusy(false); }
  }

  const expired = pairing ? pairing.expires_at * 1000 <= Date.now() : false;
  return (
    <section className="rounded-xl border p-4 space-y-4" style={{ borderColor: 'var(--color-border)', background: 'var(--color-bg-secondary)' }}>
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 className="font-semibold" style={{ color: 'var(--color-text)' }}>Mobile QR Pairing</h3>
          <p className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Create a short-lived QR code. It contains no NIM, Telegram, or OAuth secret.</p>
        </div>
        <button type="button" disabled={busy || !apiKey} onClick={() => void createPairing()} className="px-3 py-2 rounded-lg text-xs font-medium" style={{ background: 'var(--color-accent)', color: 'white', opacity: busy || !apiKey ? 0.55 : 1 }}>{busy ? 'Working…' : 'Create QR'}</button>
      </div>
      {pairing && !expired && <div className="flex flex-wrap gap-4 items-center">
        {qrImageUrl ? <img src={qrImageUrl} alt="One-time Jarivs pairing QR" className="w-44 h-44 rounded-lg bg-white p-2" /> : <div className="w-44 h-44 rounded-lg bg-white p-2 text-xs flex items-center justify-center" style={{ color: 'var(--color-text-tertiary)' }}>Loading secure QR…</div>}
        <div className="text-xs space-y-2" style={{ color: 'var(--color-text-secondary)' }}>
          <div>Expires: {new Date(pairing.expires_at * 1000).toLocaleTimeString()}</div>
          <div>Fingerprint: <code>{pairing.fingerprint}</code></div>
          <div>Device name: {pairing.device_name_hint || 'Set on phone'}</div>
          <div>Scopes: {pairing.scopes.join(', ')}</div>
          <div className="max-w-sm break-all opacity-70">Scan with Jarivs, verify the fingerprint, then approve on the phone.</div>
        </div>
      </div>}
      {pairing && expired && <p className="text-xs" style={{ color: 'var(--color-warning)' }}>This QR expired. Create a new one.</p>}
      {error && <p className="text-xs" style={{ color: 'var(--color-danger)' }}>{error}</p>}
      <div className="space-y-2">
        <div className="flex items-center justify-between"><h4 className="text-sm font-medium" style={{ color: 'var(--color-text)' }}>Registered devices</h4><button type="button" onClick={() => void loadDevices()} className="text-xs" style={{ color: 'var(--color-accent)' }}>Refresh</button></div>
        {devices.length === 0 ? <p className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>No paired devices.</p> : devices.map(device => <div key={device.device_id} className="flex items-center justify-between gap-3 rounded-lg p-2" style={{ background: 'var(--color-bg-tertiary)' }}><div><div className="text-sm" style={{ color: 'var(--color-text)' }}>{device.name}</div><div className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>{device.revoked ? 'Revoked' : device.scopes.join(', ')}</div></div><button type="button" disabled={busy || device.revoked} onClick={() => void revoke(device.device_id)} className="text-xs px-2 py-1 rounded" style={{ color: 'var(--color-danger)', border: '1px solid var(--color-danger)' }}>Revoke</button></div>)}
      </div>
      <label className="block text-xs" style={{ color: 'var(--color-text-tertiary)' }}>Device display name<input value={name} onChange={event => setName(event.target.value)} className="mt-1 w-full rounded-lg p-2 text-sm" style={{ background: 'var(--color-bg-primary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label>
      <label className="flex gap-2 items-start text-xs" style={{ color: 'var(--color-text-tertiary)' }}><input type="checkbox" checked={includeApprovals} onChange={event => setIncludeApprovals(event.target.checked)} /><span><strong style={{ color: 'var(--color-text)' }}>Allow approval review on this phone</strong><br />Optional. The phone may approve or deny only requests already created by the server; this permission is off by default.</span></label>
      <label className="flex gap-2 items-start text-xs" style={{ color: 'var(--color-text-tertiary)' }}><input type="checkbox" checked={includeLogs} onChange={event => setIncludeLogs(event.target.checked)} /><span><strong style={{ color: 'var(--color-text)' }}>Allow redacted Log Center review on this phone</strong><br />Optional and read-only. Jarivs sees registered source names plus server-redacted entries; it cannot see server paths or register sources.</span></label>
    </section>
  );
}
