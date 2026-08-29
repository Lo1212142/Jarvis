# Jarvis Windows Audio Client

هذا العميل reference client صغير لاستقبال أوامر تشغيل الصوت من OpenJarvis server. هو لا يشغل LLM ولا يحمل NIM key ولا يملك صلاحية ملفات السيرفر. يحتاج Windows 10/11 مع .NET 8 SDK، ثم يستعيد NAudio من NuGet.

## Build

```powershell
 dotnet restore .\JarvisAudioClient.csproj
 dotnet build .\JarvisAudioClient.csproj -c Release
```

## Run

شغّل الخدمة خلف HTTPS، واضبط `JARVIS_AUDIO_BASE_URL` على نفس أصل السيرفر، مثل `https://jarvis.example`. ثم مرر WebSocket URL وAPI key وclient ID:

```powershell
$env:JARVIS_AUDIO_BASE_URL = "https://jarvis.example"
dotnet run --project .\JarvisAudioClient.csproj -- "wss://jarvis.example/api/audio/ws" $env:JARVIS_WS_KEY "windows-main"
```

فعّل `audio_playback_enabled` في Settings، وأضف مجلد الموسيقى إلى `audio_allowed_roots`، ثم سجّل المسار عبر `POST /api/audio/tracks`. السيرفر يرسل `audio.play` مع stream token قصير العمر؛ العميل يفك الترميز محليًا ويطبق volume ويرسل `audio.ack`. حالة `commanded` ليست حالة تشغيل مؤكدة. إذا فشل الاتصال أو فك الترميز يرسل العميل `state=error` بدل نجاح وهمي.

لا تستخدم HTTP غير مشفر خارج loopback/private development، ولا تضع المفتاح في command history في الإنتاج؛ استخدم Windows Credential Manager أو secret injection. لا يمكن اختبار بناء هذا المشروع داخل Linux sandbox لأن .NET/Windows Media Foundation غير متوفرين هنا، لكن server protocol وstream/range/ack tests تعمل محليًا.
