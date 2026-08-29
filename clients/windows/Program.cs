using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using NAudio.Wave;

internal static class Program
{
    private static ClientWebSocket? socket;
    private static WaveOutEvent? output;
    private static IDisposable? reader;
    private static int volume = 70;

    private static async Task Main(string[] args)
    {
        if (args.Length < 3)
        {
            Console.Error.WriteLine("Usage: JarvisAudioClient <ws-url> <api-key> <client-id>");
            return;
        }

        var wsUrl = new Uri(args[0]);
        var apiKey = args[1];
        var clientId = args[2];
        socket = new ClientWebSocket();
        socket.Options.AddSubProtocol("openjarvis.auth.v1");
        socket.Options.AddSubProtocol("openjarvis.key.b64url." + Base64Url(apiKey));
        await socket.ConnectAsync(new Uri(wsUrl + "?client_id=" + Uri.EscapeDataString(clientId)), CancellationToken.None);
        Console.WriteLine("Connected to Jarvis audio channel.");
        await ReceiveLoop(clientId);
    }

    private static async Task ReceiveLoop(string clientId)
    {
        var buffer = new byte[64 * 1024];
        while (socket is { State: WebSocketState.Open })
        {
            using var message = new MemoryStream();
            WebSocketReceiveResult result;
            do
            {
                result = await socket.ReceiveAsync(buffer, CancellationToken.None);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    await socket.CloseAsync(WebSocketCloseStatus.NormalClosure, "closing", CancellationToken.None);
                    return;
                }
                message.Write(buffer, 0, result.Count);
            } while (!result.EndOfMessage);

            using var document = JsonDocument.Parse(message.ToArray());
            await HandleCommand(document.RootElement, clientId);
        }
    }

    private static async Task HandleCommand(JsonElement command, string clientId)
    {
        var type = command.TryGetProperty("type", out var typeValue) ? typeValue.GetString() : "";
        var sequence = command.TryGetProperty("sequence", out var seqValue) ? seqValue.GetInt32() : 0;
        try
        {
            switch (type)
            {
                case "audio.play":
                    var path = command.GetProperty("stream_path").GetString() ?? throw new InvalidOperationException("stream_path missing");
                    var baseUrl = Environment.GetEnvironmentVariable("JARVIS_AUDIO_BASE_URL") ?? throw new InvalidOperationException("JARVIS_AUDIO_BASE_URL is not configured");
                    await StopPlayback();
                    var media = new MediaFoundationReader(new Uri(new Uri(baseUrl), path).ToString());
                    output = new WaveOutEvent { Volume = volume / 100f };
                    output.Init(media);
                    reader = media;
                    output.Play();
                    await SendAck(clientId, sequence, "playing", "");
                    break;
                case "audio.pause":
                    output?.Pause();
                    await SendAck(clientId, sequence, "paused", "");
                    break;
                case "audio.resume":
                    output?.Play();
                    await SendAck(clientId, sequence, "playing", "");
                    break;
                case "audio.stop":
                    await StopPlayback();
                    await SendAck(clientId, sequence, "stopped", "");
                    break;
                case "audio.volume_up":
                    volume = Math.Min(100, volume + 10);
                    ApplyVolume();
                    await SendAck(clientId, sequence, "playing", "");
                    break;
                case "audio.volume_down":
                    volume = Math.Max(0, volume - 10);
                    ApplyVolume();
                    await SendAck(clientId, sequence, "playing", "");
                    break;
                case "audio.set_volume":
                    volume = Math.Clamp(command.GetProperty("value").GetInt32(), 0, 100);
                    ApplyVolume();
                    await SendAck(clientId, sequence, "playing", "");
                    break;
            }
        }
        catch (Exception ex)
        {
            await SendAck(clientId, sequence, "error", ex.GetType().Name + ": " + ex.Message);
        }
    }

    private static async Task SendAck(string clientId, int sequence, string state, string error)
    {
        if (socket is not { State: WebSocketState.Open }) return;
        var payload = JsonSerializer.Serialize(new { type = "audio.ack", client_id = clientId, sequence, state, error });
        await socket.SendAsync(Encoding.UTF8.GetBytes(payload), WebSocketMessageType.Text, true, CancellationToken.None);
    }

    private static void ApplyVolume()
    {
        if (output != null) output.Volume = volume / 100f;
    }

    private static Task StopPlayback()
    {
        output?.Stop();
        output?.Dispose();
        output = null;
        reader?.Dispose();
        reader = null;
        return Task.CompletedTask;
    }

    private static string Base64Url(string value) => Convert.ToBase64String(Encoding.UTF8.GetBytes(value)).TrimEnd('=').Replace('+', '-').Replace('/', '_');
}
