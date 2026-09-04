/**
 * Creative Studio page — media provider settings, gallery & health.
 *
 * Fully additive: talks only to the /api/creative/* routes mounted by the
 * creative suite. Three tabs:
 *  - Providers: pick image/video generation provider + model + key
 *  - Gallery: browse generated media (images / videos / audio)
 *  - Health: self-healing watcher status and repairs
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Clapperboard,
  Key,
  RefreshCw,
  Check,
  X,
  Save,
  RotateCcw,
  ImageIcon,
  Film,
  Music,
  Activity,
  Wand2,
} from 'lucide-react';
import { apiFetch } from '../lib/api';
import { Button } from '../components/ui/button';
import { Input } from '../components/ui/input';

// ---------------------------------------------------------------------------
// Types (mirrors openjarvis.creative.media_settings)
// ---------------------------------------------------------------------------

interface ProviderConfig {
  label: string;
  mode: string;
  base_url: string;
  model: string;
  api_key_env?: string;
  models?: string[];
  extra?: Record<string, unknown>;
}

interface KeyStatus {
  configured: boolean;
  source: string;
  masked: string;
  api_key_env: string;
}

interface CreativeSettings {
  image_generation: {
    default_provider: string;
    providers: Record<string, ProviderConfig>;
  };
  video_generation: {
    default_provider: string;
    providers: Record<string, ProviderConfig>;
  };
  editing: {
    engine: string;
    canvas: string;
    fps: number;
    quality: string;
  };
}

interface GalleryItem {
  kind: 'image' | 'video' | 'audio';
  name: string;
  url: string;
  size_bytes: number;
  modified: number;
}

interface HealthPayload {
  watcher_running: boolean;
  repairs?: Record<string, number>;
  checks?: Record<string, boolean>;
  agents_tracked?: string[];
  guardian?: {
    command?: string | null;
    supervised?: boolean;
    restarts?: number | null;
    crashes?: number | null;
    hang_kills?: number | null;
    boot_failures?: number | null;
    recovery_runs?: number | null;
    circuit_open?: boolean;
    last_classification?: string | null;
    heartbeat_age_s?: number;
    server_pid?: number | null;
    error?: string;
  };
}

type Tab = 'providers' | 'gallery' | 'health';

const TABS: { id: Tab; label: string; icon: typeof Wand2 }[] = [
  { id: 'providers', label: 'Providers', icon: Wand2 },
  { id: 'gallery', label: 'Gallery', icon: ImageIcon },
  { id: 'health', label: 'Health', icon: Activity },
];

const QUALITIES = ['fast', 'balanced', 'high'];

// ---------------------------------------------------------------------------
// Provider card
// ---------------------------------------------------------------------------

function ProviderCard({
  capability,
  section,
  settings,
  keyStatus,
  onSaved,
}: {
  capability: 'image' | 'video';
  section: 'image_generation' | 'video_generation';
  settings: CreativeSettings;
  keyStatus: Record<string, KeyStatus> | undefined;
  onSaved: (s: CreativeSettings, k: Record<string, unknown>) => void;
}) {
  const gen = settings[section];
  const [selected, setSelected] = useState(gen.default_provider);
  const [model, setModel] = useState(
    gen.providers[gen.default_provider]?.model ?? '',
  );
  const [baseUrl, setBaseUrl] = useState(
    gen.providers[gen.default_provider]?.base_url ?? '',
  );
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState('');

  const provider = gen.providers[selected];
  const status = keyStatus?.[selected];

  useEffect(() => {
    setModel(gen.providers[selected]?.model ?? '');
    setBaseUrl(gen.providers[selected]?.base_url ?? '');
    setSaved(false);
    setError('');
  }, [selected, gen.providers]);

  const save = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const res = await apiFetch(`/api/creative/settings/${section}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          patch: {
            default_provider: selected,
            providers: {
              [selected]: { model, base_url: baseUrl },
            },
          },
        }),
      });
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
      const data = await res.json();
      onSaved(data.settings as CreativeSettings, data.key_status);
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [section, selected, model, baseUrl, onSaved]);

  const saveKey = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const res = await apiFetch(`/api/creative/keys/${selected}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!res.ok) throw new Error((await res.text()) || `HTTP ${res.status}`);
      const data = await res.json();
      onSaved(settings, data.key_status);
      setApiKey('');
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, [selected, apiKey, onSaved, settings]);

  return (
    <div className="rounded-xl border border-border bg-card p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold flex items-center gap-2">
          {capability === 'image' ? (
            <ImageIcon className="size-4" />
          ) : (
            <Film className="size-4" />
          )}
          {capability === 'image' ? 'Image Generation' : 'Video Generation'}
        </h3>
        <span
          className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${
            status?.configured
              ? 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
              : 'bg-muted text-muted-foreground'
          }`}
        >
          {status?.configured ? (
            <Check className="size-3" />
          ) : (
            <X className="size-3" />
          )}
          {status?.configured
            ? `key: ${status.source}`
            : 'no API key'}
        </span>
      </div>

      <label className="block text-xs text-muted-foreground">Provider</label>
      <select
        className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
        value={selected}
        onChange={(e) => setSelected(e.target.value)}
      >
        {Object.entries(gen.providers).map(([name, cfg]) => (
          <option key={name} value={name}>
            {cfg.label || name}
          </option>
        ))}
      </select>

      <label className="block text-xs text-muted-foreground">Model</label>
      <Input
        list={`models-${section}`}
        value={model}
        onChange={(e) => setModel(e.target.value)}
        placeholder="model id"
      />
      <datalist id={`models-${section}`}>
        {(provider?.models ?? []).map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {(provider?.models ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1">
          {provider!.models!.slice(0, 4).map((m) => (
            <button
              key={m}
              onClick={() => setModel(m)}
              className="rounded-full bg-muted px-2 py-0.5 text-[11px] text-muted-foreground hover:text-foreground"
            >
              {m.split('/').pop()}
            </button>
          ))}
        </div>
      )}

      <label className="block text-xs text-muted-foreground">Base URL</label>
      <Input
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.target.value)}
        placeholder="https://…"
      />

      <label className="block text-xs text-muted-foreground">
        API key {provider?.api_key_env ? `(env: ${provider.api_key_env})` : ''}
      </label>
      <div className="flex gap-2">
        <Input
          type="password"
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
          placeholder={status?.masked || 'paste key…'}
        />
        <Button variant="outline" size="sm" onClick={saveKey} disabled={busy || !apiKey}>
          <Key className="size-3.5" /> Save key
        </Button>
      </div>

      <div className="flex items-center gap-2 pt-1">
        <Button size="sm" onClick={save} disabled={busy}>
          <Save className="size-3.5" /> Save settings
        </Button>
        {saved && (
          <span className="text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1">
            <Check className="size-3" /> saved
          </span>
        )}
        {error && <span className="text-xs text-destructive">{error}</span>}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Gallery
// ---------------------------------------------------------------------------

function fmtSize(bytes: number): string {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

function GalleryGrid() {
  const [items, setItems] = useState<GalleryItem[]>([]);
  const [filter, setFilter] = useState<'all' | 'image' | 'video' | 'audio'>('all');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await apiFetch('/api/creative/gallery?limit=60');
      if (res.ok) {
        const data = await res.json();
        setItems(data.items ?? []);
      }
    } catch {
      /* server unreachable — keep previous items */
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const shown = useMemo(
    () => (filter === 'all' ? items : items.filter((i) => i.kind === filter)),
    [items, filter],
  );

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        {(['all', 'image', 'video', 'audio'] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`rounded-full px-3 py-1 text-xs capitalize ${
              filter === f
                ? 'bg-primary text-primary-foreground'
                : 'bg-muted text-muted-foreground hover:text-foreground'
            }`}
          >
            {f}
          </button>
        ))}
        <div className="flex-1" />
        <Button variant="ghost" size="sm" onClick={load}>
          <RefreshCw className="size-3.5" /> Refresh
        </Button>
      </div>

      {loading && items.length === 0 ? (
        <p className="text-sm text-muted-foreground py-8 text-center">
          Loading media…
        </p>
      ) : shown.length === 0 ? (
        <p className="text-sm text-muted-foreground py-8 text-center">
          No media yet — ask Jarvis in chat to generate or edit something
          (“generate an image of …”, “make a demo video about …”).
        </p>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {shown.map((item) => (
            <a
              key={item.url}
              href={item.url}
              target="_blank"
              rel="noreferrer"
              className="group block overflow-hidden rounded-lg border border-border bg-muted/40"
            >
              {item.kind === 'image' ? (
                <img
                  src={item.url}
                  alt={item.name}
                  loading="lazy"
                  className="aspect-video w-full object-cover transition group-hover:scale-[1.02]"
                />
              ) : item.kind === 'video' ? (
                <video
                  src={item.url}
                  controls
                  preload="metadata"
                  className="aspect-video w-full bg-black object-contain"
                />
              ) : (
                <div className="flex aspect-video w-full items-center justify-center bg-muted">
                  <Music className="size-8 text-muted-foreground" />
                </div>
              )}
              <div className="p-2">
                <p className="truncate text-xs font-medium">{item.name}</p>
                <p className="text-[11px] text-muted-foreground">
                  {item.kind} · {fmtSize(item.size_bytes)}
                </p>
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Health
// ---------------------------------------------------------------------------

function Stat({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">
        {value == null ? '—' : String(value)}
      </span>
    </div>
  );
}

function HealthPanel() {
  const [health, setHealth] = useState<HealthPayload | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await apiFetch('/api/creative/health');
      if (res.ok) setHealth(await res.json());
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 15000);
    return () => clearInterval(timer);
  }, [load]);

  if (!health) {
    return (
      <p className="text-sm text-muted-foreground py-8 text-center">
        Loading health…
      </p>
    );
  }
  const repairs = Object.entries(health.repairs ?? {});
  const checks = Object.entries(health.checks ?? {});

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-border bg-card p-4">
        <div className="flex items-center gap-2">
          <span
            className={`inline-flex size-2 rounded-full ${
              health.watcher_running ? 'bg-emerald-500' : 'bg-amber-500'
            }`}
          />
          <span className="text-sm font-medium">
            Self-healing watcher {health.watcher_running ? 'running' : 'idle'}
          </span>
        </div>
        <p className="mt-1 text-xs text-muted-foreground">
          Monitors tool failures, repairs corrupted settings, restores
          self-developed tools from baseline and keeps render scratch space
          clean.
        </p>
        {health.agents_tracked?.length ? (
          <p className="mt-2 text-xs text-muted-foreground">
            Agents tracked: {health.agents_tracked.join(', ')}
          </p>
        ) : null}
      </div>

      {health.guardian && !health.guardian.error ? (
        <div className="rounded-xl border border-border bg-card p-4">
          <div className="flex items-center gap-2">
            <span
              className={`inline-flex size-2 rounded-full ${
                health.guardian.circuit_open
                  ? 'bg-destructive'
                  : health.guardian.supervised
                    ? 'bg-emerald-500'
                    : 'bg-amber-500'
              }`}
            />
            <span className="text-sm font-medium">
              Guardian{' '}
              {health.guardian.circuit_open
                ? 'circuit breaker open'
                : health.guardian.supervised
                  ? 'supervising'
                  : 'not supervising (run guardian.py)'}
            </span>
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            Whole-process self-recovery: restarts the server on crash, kills it
            when the heartbeat goes stale, and repairs the environment when
            boot keeps failing.
          </p>
          <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-xs sm:grid-cols-3">
            <Stat label="restarts" value={health.guardian.restarts} />
            <Stat label="crashes" value={health.guardian.crashes} />
            <Stat label="hang kills" value={health.guardian.hang_kills} />
            <Stat label="boot fails" value={health.guardian.boot_failures} />
            <Stat label="recoveries" value={health.guardian.recovery_runs} />
            <Stat
              label="heartbeat"
              value={
                health.guardian.heartbeat_age_s != null
                  ? `${health.guardian.heartbeat_age_s}s ago`
                  : '—'
              }
            />
          </div>
          {health.guardian.last_classification ? (
            <p className="mt-2 text-xs text-muted-foreground">
              Last event: {health.guardian.last_classification}
              {health.guardian.server_pid
                ? ` · server pid ${health.guardian.server_pid}`
                : ''}
            </p>
          ) : null}
        </div>
      ) : null}

      {checks.length > 0 && (
        <div className="rounded-xl border border-border bg-card p-4">
          <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Health checks
          </h4>
          <div className="grid gap-1.5 sm:grid-cols-2">
            {checks.map(([name, ok]) => (
              <div key={name} className="flex items-center gap-2 text-sm">
                {ok ? (
                  <Check className="size-3.5 text-emerald-500" />
                ) : (
                  <X className="size-3.5 text-destructive" />
                )}
                <span className="truncate">{name}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="rounded-xl border border-border bg-card p-4">
        <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Repairs performed
        </h4>
        {repairs.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            None so far — everything is healthy.
          </p>
        ) : (
          <ul className="space-y-1 text-sm">
            {repairs.map(([name, count]) => (
              <li key={name} className="flex justify-between">
                <span className="truncate">{name}</span>
                <span className="text-muted-foreground">{count}×</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function CreativeStudioPage() {
  const [tab, setTab] = useState<Tab>('providers');
  const [settings, setSettings] = useState<CreativeSettings | null>(null);
  const [keyStatus, setKeyStatus] = useState<
    Record<string, Record<string, KeyStatus>> | undefined
  >();
  const [canvas, setCanvas] = useState('1920x1080');
  const [fps, setFps] = useState(30);
  const [quality, setQuality] = useState('high');

  const load = useCallback(async () => {
    try {
      const res = await apiFetch('/api/creative/settings');
      if (res.ok) {
        const data = await res.json();
        setSettings(data.settings);
        setKeyStatus(data.key_status);
        setCanvas(data.settings.editing.canvas);
        setFps(data.settings.editing.fps);
        setQuality(data.settings.editing.quality);
      }
    } catch {
      /* server not running */
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onSaved = useCallback(
    (s: CreativeSettings, k: Record<string, unknown>) => {
      setSettings(s);
      setKeyStatus(k as Record<string, Record<string, KeyStatus>>);
    },
    [],
  );

  const saveEditing = useCallback(async () => {
    try {
      const res = await apiFetch('/api/creative/settings/editing', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          patch: { canvas, fps: Number(fps), quality },
        }),
      });
      if (res.ok) {
        const data = await res.json();
        setSettings(data.settings);
      }
    } catch {
      /* ignore */
    }
  }, [canvas, fps, quality]);

  const resetProviders = useCallback(async () => {
    try {
      await apiFetch('/api/creative/settings/image_generation/reset', {
        method: 'POST',
      });
      await apiFetch('/api/creative/settings/video_generation/reset', {
        method: 'POST',
      });
      load();
    } catch {
      /* ignore */
    }
  }, [load]);

  return (
    <div className="mx-auto max-w-4xl px-4 py-6 space-y-6">
      <header className="flex items-center gap-3">
        <div className="flex size-10 items-center justify-center rounded-xl bg-primary/10">
          <Clapperboard className="size-5 text-primary" />
        </div>
        <div>
          <h1 className="text-lg font-semibold">Creative Studio</h1>
          <p className="text-sm text-muted-foreground">
            Media generation providers, editing defaults, gallery and
            self-healing status.
          </p>
        </div>
        <div className="flex-1" />
        <Button variant="ghost" size="sm" onClick={load}>
          <RefreshCw className="size-3.5" /> Reload
        </Button>
        <Button variant="outline" size="sm" onClick={resetProviders}>
          <RotateCcw className="size-3.5" /> Reset providers
        </Button>
      </header>

      <nav className="flex gap-1 rounded-lg bg-muted p-1">
        {TABS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => setTab(id)}
            className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-3 py-1.5 text-sm ${
              tab === id
                ? 'bg-background shadow-sm font-medium'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            <Icon className="size-3.5" /> {label}
          </button>
        ))}
      </nav>

      {tab === 'providers' &&
        (settings ? (
          <div className="space-y-4">
            <div className="grid gap-4 lg:grid-cols-2">
              <ProviderCard
                capability="image"
                section="image_generation"
                settings={settings}
                keyStatus={keyStatus?.image}
                onSaved={onSaved}
              />
              <ProviderCard
                capability="video"
                section="video_generation"
                settings={settings}
                keyStatus={keyStatus?.video}
                onSaved={onSaved}
              />
            </div>

            <div className="rounded-xl border border-border bg-card p-4 space-y-3">
              <h3 className="text-sm font-semibold">Editing defaults</h3>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-xs text-muted-foreground">
                    Canvas
                  </label>
                  <Input
                    value={canvas}
                    onChange={(e) => setCanvas(e.target.value)}
                    placeholder="1920x1080"
                  />
                </div>
                <div>
                  <label className="block text-xs text-muted-foreground">
                    FPS
                  </label>
                  <Input
                    type="number"
                    value={fps}
                    onChange={(e) => setFps(Number(e.target.value))}
                    min={1}
                    max={120}
                  />
                </div>
                <div>
                  <label className="block text-xs text-muted-foreground">
                    Quality
                  </label>
                  <select
                    className="w-full rounded-lg border border-border bg-background px-3 py-2 text-sm"
                    value={quality}
                    onChange={(e) => setQuality(e.target.value)}
                  >
                    {QUALITIES.map((q) => (
                      <option key={q} value={q}>
                        {q}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <Button size="sm" onClick={saveEditing}>
                <Save className="size-3.5" /> Save defaults
              </Button>
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground py-8 text-center">
            Connect to the server to configure providers…
          </p>
        ))}

      {tab === 'gallery' && <GalleryGrid />}
      {tab === 'health' && <HealthPanel />}
    </div>
  );
}
