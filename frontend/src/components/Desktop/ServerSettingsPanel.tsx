import { useCallback, useEffect, useState } from 'react';
import { apiFetch } from '../../lib/api';

type ServerSettings = {
  llm_provider: string;
  llm_model: string;
  nim_rpm_limit: number;
  speech_stt_backend: string;
  speech_tts_backend: string;
  wake_word_enabled: boolean;
  wake_words: string[];
  wake_word_language: string;
  wake_word_sensitivity: number;
  push_to_talk_enabled: boolean;
  audio_hot_window_seconds: number;
  telegram_enabled: boolean;
  telegram_allowed_chat_ids: string[];
  proactive_enabled: boolean;
  proactive_voice_enabled: boolean;
  proactive_quiet_start: string;
  proactive_quiet_end: string;
  proactive_cooldown_minutes: number;
  proactive_daily_cap: number;
  humor_enabled: boolean;
  humor_style: string;
  sarcasm_level: string;
  predictive_suggestions: boolean;
  max_predictive_suggestions: number;
  browser_enabled: boolean;
  browser_profile: string;
  sandbox_enabled: boolean;
  sandbox_network: boolean;
  sandbox_memory_mb: number;
  sandbox_timeout_seconds: number;
  resource_monitor_enabled: boolean;
  resource_poll_interval_seconds: number;
  resource_cpu_alert_percent: number;
  resource_memory_alert_percent: number;
  resource_alert_cooldown_seconds: number;
};

type SettingsResponse = { settings: ServerSettings; credentials: Record<string, boolean> };
type ResourceSnapshot = { timestamp: number; process_cpu_percent: number | null; process_rss_mb: number | null; process_memory_percent: number | null; system_cpu_percent: number | null; system_memory_percent: number | null; system_memory_available_mb: number | null; measurement_available: boolean; errors: string[] };
type ResourceResponse = { snapshot: ResourceSnapshot; monitor: { enabled: boolean; poll_interval_seconds: number; cpu_alert_percent: number; memory_alert_percent: number; alert_cooldown_seconds: number }; alerts: Array<{ kind: string; value: number; threshold: number; message: string; timestamp: number }> };
type Capability = { key: string; name: string; description: string; risk: string; requires_approval: boolean; sandbox_required: boolean };

function Toggle({ value, onChange }: { value: boolean; onChange: (value: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      aria-pressed={value}
      className="relative h-6 w-11 rounded-full transition-colors cursor-pointer"
      style={{ background: value ? 'var(--color-accent)' : 'var(--color-bg-tertiary)' }}
    >
      <span
        className="absolute top-0.5 h-5 w-5 rounded-full bg-white transition-transform"
        style={{ left: value ? 22 : 2 }}
      />
    </button>
  );
}

export function ServerSettingsPanel() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [draft, setDraft] = useState<Partial<ServerSettings>>({});
  const [message, setMessage] = useState('');
  const [loading, setLoading] = useState(true);
  const [resource, setResource] = useState<ResourceResponse | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await apiFetch('/api/settings');
      if (!response.ok) throw new Error(`Server settings unavailable (${response.status})`);
      const next = await response.json() as SettingsResponse;
      setData(next);
      const capabilityResponse = await apiFetch('/api/settings/capabilities');
      if (capabilityResponse.ok) {
        const capabilityPayload = await capabilityResponse.json() as { capabilities: Capability[] };
        setCapabilities(capabilityPayload.capabilities || []);
      }
      setDraft(next.settings);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Could not load server settings');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const refreshResources = useCallback(async () => {
    try {
      const response = await apiFetch('/api/resources/current');
      if (response.ok) setResource(await response.json() as ResourceResponse);
    } catch {
      // The panel keeps the last truthful measurement; it never substitutes zero.
    }
  }, []);

  useEffect(() => {
    void refreshResources();
    const timer = window.setInterval(() => void refreshResources(), 5000);
    return () => window.clearInterval(timer);
  }, [refreshResources]);

  const update = <K extends keyof ServerSettings>(key: K, value: ServerSettings[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const save = async () => {
    try {
      const response = await apiFetch('/api/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(draft),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || 'Could not save server settings');
      setData(payload);
      setDraft(payload.settings);
      setMessage('Saved. Restart the server to apply engine and speech changes.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Could not save server settings');
    }
  };

  if (loading) return <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>Loading server controls…</div>;
  if (!data) return <div className="text-xs" style={{ color: 'var(--color-error)' }}>{message || 'Server controls unavailable'}</div>;

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          LLM provider
          <select value={draft.llm_provider || ''} onChange={(event) => update('llm_provider', event.target.value)} className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}>
            <option value="nim">NVIDIA NIM</option><option value="ollama">Ollama</option><option value="openai_compatible">OpenAI-compatible</option>
          </select>
        </label>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          Model ID
          <input value={draft.llm_model || ''} onChange={(event) => update('llm_model', event.target.value)} placeholder="provider model id" className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        </label>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          NIM maximum RPM (hard ceiling 40)
          <input type="number" min={1} max={40} value={draft.nim_rpm_limit ?? 40} onChange={(event) => update('nim_rpm_limit', Math.min(40, Math.max(1, Number(event.target.value))))} className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        </label>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>
          Browser profile
          <input value={draft.browser_profile || ''} onChange={(event) => update('browser_profile', event.target.value)} className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        </label>
      </div>

      <div className="rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}>
        <div className="flex items-center justify-between gap-3"><div><div className="text-sm" style={{ color: 'var(--color-text)' }}>Server resource monitor</div><div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>Real process measurements only. No guessed values are displayed.</div></div><Toggle value={draft.resource_monitor_enabled !== false} onChange={(value) => update('resource_monitor_enabled', value)} /></div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-2 mt-3"><div className="rounded-md p-2" style={{ background: 'var(--color-bg)' }}><div className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>Jarvis CPU</div><div className="text-lg" style={{ color: 'var(--color-text)' }}>{resource?.snapshot.measurement_available && resource.snapshot.process_cpu_percent !== null ? `${resource.snapshot.process_cpu_percent.toFixed(1)}%` : 'Unavailable'}</div></div><div className="rounded-md p-2" style={{ background: 'var(--color-bg)' }}><div className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>Jarvis RAM</div><div className="text-lg" style={{ color: 'var(--color-text)' }}>{resource?.snapshot.measurement_available && resource.snapshot.process_rss_mb !== null ? `${resource.snapshot.process_rss_mb.toFixed(1)} MB` : 'Unavailable'}</div></div><div className="rounded-md p-2" style={{ background: 'var(--color-bg)' }}><div className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>Host CPU</div><div className="text-lg" style={{ color: 'var(--color-text)' }}>{resource?.snapshot.system_cpu_percent !== null && resource?.snapshot.system_cpu_percent !== undefined ? `${resource.snapshot.system_cpu_percent.toFixed(1)}%` : 'Unavailable'}</div></div><div className="rounded-md p-2" style={{ background: 'var(--color-bg)' }}><div className="text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>Host RAM</div><div className="text-lg" style={{ color: 'var(--color-text)' }}>{resource?.snapshot.system_memory_percent !== null && resource?.snapshot.system_memory_percent !== undefined ? `${resource.snapshot.system_memory_percent.toFixed(1)}%` : 'Unavailable'}</div></div></div>
        <div className="grid grid-cols-1 md:grid-cols-4 gap-2 mt-3"><label className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>Poll seconds<input type="number" min={1} max={3600} value={draft.resource_poll_interval_seconds ?? 5} onChange={(event) => update('resource_poll_interval_seconds', Number(event.target.value))} className="mt-1 w-full rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label><label className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>CPU alert %<input type="number" min={1} max={1000} value={draft.resource_cpu_alert_percent ?? 85} onChange={(event) => update('resource_cpu_alert_percent', Number(event.target.value))} className="mt-1 w-full rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label><label className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>RAM alert %<input type="number" min={1} max={100} value={draft.resource_memory_alert_percent ?? 85} onChange={(event) => update('resource_memory_alert_percent', Number(event.target.value))} className="mt-1 w-full rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label><label className="text-[11px]" style={{ color: 'var(--color-text-secondary)' }}>Cooldown seconds<input type="number" min={0} max={86400} value={draft.resource_alert_cooldown_seconds ?? 120} onChange={(event) => update('resource_alert_cooldown_seconds', Number(event.target.value))} className="mt-1 w-full rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label></div>
        {resource?.alerts?.length ? <div className="mt-2 text-xs" style={{ color: 'var(--color-warning)' }}>Alert: {resource.alerts[resource.alerts.length - 1].message}</div> : null}
        <div className="mt-2 text-[10px]" style={{ color: 'var(--color-text-tertiary)' }}>{resource?.snapshot.measurement_available ? `Measured ${new Date(resource.snapshot.timestamp * 1000).toLocaleTimeString()}` : 'Measurement unavailable; the assistant must say it cannot measure rather than guess.'}</div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div className="rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}>
          <div className="flex items-center justify-between"><span className="text-sm" style={{ color: 'var(--color-text)' }}>Telegram channel</span><Toggle value={!!draft.telegram_enabled} onChange={(value) => update('telegram_enabled', value)} /></div>
          <p className="text-[11px] mt-1" style={{ color: 'var(--color-text-tertiary)' }}>{data.credentials.TELEGRAM_BOT_TOKEN ? 'Bot token present on server' : 'Bot token is not configured'}</p>
          <input value={(draft.telegram_allowed_chat_ids || []).join(', ')} onChange={(event) => update('telegram_allowed_chat_ids', event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} placeholder="Allowed chat IDs, comma separated" className="mt-2 w-full rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} />
        </div>
        <div className="rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}>
          <div className="flex items-center justify-between"><span className="text-sm" style={{ color: 'var(--color-text)' }}>Proactive check-ins</span><Toggle value={!!draft.proactive_enabled} onChange={(value) => update('proactive_enabled', value)} /></div>
          <div className="flex items-center justify-between mt-2"><span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Voice mode</span><Toggle value={!!draft.proactive_voice_enabled} onChange={(value) => update('proactive_voice_enabled', value)} /></div>
          <div className="grid grid-cols-2 gap-2 mt-2"><input value={draft.proactive_quiet_start || ''} onChange={(event) => update('proactive_quiet_start', event.target.value)} placeholder="22:00" className="rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><input value={draft.proactive_quiet_end || ''} onChange={(event) => update('proactive_quiet_end', event.target.value)} placeholder="08:00" className="rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></div>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}><div className="flex items-center justify-between"><span className="text-sm" style={{ color: 'var(--color-text)' }}>Humor and sarcasm</span><Toggle value={!!draft.humor_enabled} onChange={(value) => update('humor_enabled', value)} /></div><div className="grid grid-cols-2 gap-2 mt-2"><select value={draft.humor_style || 'dry'} onChange={(event) => update('humor_style', event.target.value)} className="rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}><option value="dry">Dry</option><option value="witty">Witty</option><option value="warm">Warm</option><option value="formal">Formal</option></select><select value={draft.sarcasm_level || 'light'} onChange={(event) => update('sarcasm_level', event.target.value)} className="rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}><option value="none">None</option><option value="light">Light</option><option value="medium">Medium</option></select></div></div>
        <div className="rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}><div className="flex items-center justify-between"><span className="text-sm" style={{ color: 'var(--color-text)' }}>Predictive suggestions</span><Toggle value={!!draft.predictive_suggestions} onChange={(value) => update('predictive_suggestions', value)} /></div><div className="text-[11px] mt-1" style={{ color: 'var(--color-text-tertiary)' }}>Suggestions only; external actions still require approval.</div><input type="number" min={0} max={5} value={draft.max_predictive_suggestions ?? 2} onChange={(event) => update('max_predictive_suggestions', Math.min(5, Math.max(0, Number(event.target.value))))} className="mt-2 w-full rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></div>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>STT backend<select value={draft.speech_stt_backend || ''} onChange={(event) => update('speech_stt_backend', event.target.value)} className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}><option value="openai">OpenAI</option><option value="deepgram">Deepgram</option><option value="faster-whisper">Local faster-whisper</option></select></label>
        <div className="rounded-lg p-3 md:col-span-2" style={{ background: 'var(--color-bg-secondary)' }}><div className="flex items-center justify-between"><div><div className="text-sm" style={{ color: 'var(--color-text)' }}>Wake word and microphone</div><div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>The Windows client sends audio only after wake word or push-to-talk.</div></div><Toggle value={!!draft.wake_word_enabled} onChange={(value) => update('wake_word_enabled', value)} /></div><div className="grid grid-cols-1 md:grid-cols-4 gap-2 mt-2"><input value={(draft.wake_words || []).join(', ')} onChange={(event) => update('wake_words', event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} placeholder="Jarvis, يا جارفيس" className="rounded-lg px-2 py-2 text-xs md:col-span-2" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /><select value={draft.wake_word_language || 'multi'} onChange={(event) => update('wake_word_language', event.target.value)} className="rounded-lg px-2 py-2 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}><option value="auto">Auto</option><option value="ar">Arabic</option><option value="en">English</option><option value="multi">Arabic + English</option></select><input type="number" min={0} max={1} step={0.05} value={draft.wake_word_sensitivity ?? 0.75} onChange={(event) => update('wake_word_sensitivity', Math.min(1, Math.max(0, Number(event.target.value))))} className="rounded-lg px-2 py-2 text-xs" aria-label="Wake word sensitivity" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></div><div className="flex items-center justify-between mt-2"><span className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Push-to-talk fallback</span><Toggle value={!!draft.push_to_talk_enabled} onChange={(value) => update('push_to_talk_enabled', value)} /><label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Hot window seconds<input type="number" min={1} max={60} value={draft.audio_hot_window_seconds ?? 8} onChange={(event) => update('audio_hot_window_seconds', Math.min(60, Math.max(1, Number(event.target.value))))} className="ml-2 w-20 rounded-lg px-2 py-1 text-xs" style={{ background: 'var(--color-bg)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label></div></div>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>TTS backend<select value={draft.speech_tts_backend || ''} onChange={(event) => update('speech_tts_backend', event.target.value)} className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }}><option value="openai">OpenAI</option><option value="cartesia">Cartesia</option><option value="kokoro">Local Kokoro</option></select></label>
        <label className="text-xs" style={{ color: 'var(--color-text-secondary)' }}>Sandbox memory MB<input type="number" min={64} max={16384} value={draft.sandbox_memory_mb ?? 512} onChange={(event) => update('sandbox_memory_mb', Number(event.target.value))} className="mt-1 w-full rounded-lg px-2 py-2" style={{ background: 'var(--color-bg-secondary)', color: 'var(--color-text)', border: '1px solid var(--color-border)' }} /></label>
      </div>

      <div className="flex items-center justify-between rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}><div><div className="text-sm" style={{ color: 'var(--color-text)' }}>Sandbox network</div><div className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>Keep disabled unless a reviewed tool explicitly needs network access.</div></div><Toggle value={!!draft.sandbox_network} onChange={(value) => update('sandbox_network', value)} /></div>
      <div className="flex items-center gap-3"><button type="button" onClick={() => void save()} className="rounded-lg px-3 py-2 text-xs font-medium cursor-pointer" style={{ background: 'var(--color-accent)', color: 'white' }}>Save server settings</button><span className="text-xs" style={{ color: message.startsWith('Saved') ? 'var(--color-success)' : 'var(--color-text-tertiary)' }}>{message}</span></div>
      <div className="rounded-lg p-3" style={{ background: 'var(--color-bg-secondary)' }}><div className="text-sm mb-2" style={{ color: 'var(--color-text)' }}>Capability catalog</div><div className="grid grid-cols-1 md:grid-cols-2 gap-2">{capabilities.map((capability) => <div key={capability.key} className="rounded-md p-2" style={{ background: 'var(--color-bg)', border: '1px solid var(--color-border)' }}><div className="flex items-center justify-between gap-2"><span className="text-xs" style={{ color: 'var(--color-text)' }}>{capability.name}</span><span className="text-[10px] uppercase" style={{ color: capability.risk === 'blocked' ? 'var(--color-error)' : capability.risk === 'high' ? 'var(--color-warning)' : 'var(--color-text-tertiary)' }}>{capability.risk}</span></div><div className="text-[10px] mt-1" style={{ color: 'var(--color-text-tertiary)' }}>{capability.description}</div><div className="text-[10px] mt-1" style={{ color: 'var(--color-text-tertiary)' }}>{capability.requires_approval ? 'Approval required' : 'No approval by default'}{capability.sandbox_required ? ' · sandbox' : ''}</div></div>)}</div></div>
      <p className="text-[11px]" style={{ color: 'var(--color-text-tertiary)' }}>API keys are intentionally not rendered or stored by this page. Configure them in the server secret environment/vault; the status above only reports whether a key exists.</p>
    </div>
  );
}
