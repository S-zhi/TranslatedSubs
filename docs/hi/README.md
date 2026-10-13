[English](../../README.md) | [简体中文](../zh/README.md) | हिन्दी | [Español](../es/README.md) | [Français](../fr/README.md) | [Português](../pt/README.md) | [Русский](../ru/README.md)

<div align="center">
  <img src="../assets/eye-subtitles-logo.svg" width="88" alt="TranslatedSubs लोगो" />
  <h1>TranslatedSubs</h1>
  <p><strong>वीडियो में बोली और स्क्रीन पर दिखने वाला पाठ, दोनों समझें: वाणी का प्रतिलेखन, सबटाइटल अनुवाद और बोर्ड पर लिखी सामग्री की पहचान।</strong></p>
  <p>तेज़ सेटअप और MCP एकीकरण।</p>
</div>

TranslatedSubs वीडियो और ऑडियो को समझने का कार्यक्षेत्र है: मीडिया डाउनलोड करें, वाणी का प्रतिलेखन और सबटाइटल अनुवाद करें, तथा वीडियो में दिखाई देने वाली लिखित सामग्री पहचानें।

- **कई स्रोतों से वीडियो**: समर्थित प्लेटफ़ॉर्म से सीधे डाउनलोड करें; फ़ाइल को पहले डाउनलोड करके फिर अपलोड करने की ज़रूरत नहीं।
- **लचीला स्थानीय सेटअप और मॉडल विकल्प**: तेज़ी से शुरू करें या हल्का सेटअप चुनें; बेहतर मिलान के लिए उन्नत और समुदाय के मॉडल कॉन्फ़िगर करें।
- **समानांतर कार्य और संसाधन जानकारी**: एक साथ कई कार्य चलाएँ और उनकी स्थिति व संसाधन उपयोग देखें। Google Drive सिंक भी उपलब्ध है।
- **अलग मॉडल फ़ाइन-ट्यूनिंग सेवा**: समर्थित मॉडल को अलग सेवा के माध्यम से फ़ाइन-ट्यून करके परिणाम बेहतर करें।
- **कई डिवाइस पर उपयोग**: वेब, Windows और macOS पर उपलब्ध; मोबाइल समर्थन भविष्य के लिए नियोजित है।

समर्थित वीडियो प्लेटफ़ॉर्म:
[![YouTube](https://img.shields.io/badge/YouTube-FF0033?style=plastic&logo=youtube&logoColor=white)](https://www.youtube.com/)
[![Vimeo](https://img.shields.io/badge/Vimeo-1AB7EA?style=plastic&logo=vimeo&logoColor=white)](https://vimeo.com/)
[![Dailymotion](https://img.shields.io/badge/Dailymotion-0066DC?style=plastic&logo=dailymotion&logoColor=white)](https://www.dailymotion.com/)
[![Twitch](https://img.shields.io/badge/Twitch-9146FF?style=plastic&logo=twitch&logoColor=white)](https://www.twitch.tv/)
[![TikTok](https://img.shields.io/badge/TikTok-111111?style=plastic&logo=tiktok&logoColor=white)](https://www.tiktok.com/)
[![X / Twitter](https://img.shields.io/badge/X%20%28Twitter%29-111111?style=plastic&logo=x&logoColor=white)](https://x.com/)
[![Instagram](https://img.shields.io/badge/Instagram-E4405F?style=plastic&logo=instagram&logoColor=white)](https://www.instagram.com/)
[![AcFun](https://img.shields.io/badge/AcFun-FD4C5D?style=plastic)](https://www.acfun.cn/)
[![Niconico](https://img.shields.io/badge/Niconico-252525?style=plastic&logo=niconico&logoColor=white)](https://www.nicovideo.jp/)
[![Pornhub](https://img.shields.io/badge/Pornhub-FF9900?style=plastic)](https://www.pornhub.com/)

![TranslatedSubs](../assets/readme-demo-1.png)

![TranslatedSubs](../assets/readme-demo-2.png)

## सोर्स कोड से बिल्ड करें

### ज़रूरी निर्भरताएँ

सोर्स कोड से चलाने के लिए Python 3.10–3.12, `uv`, Git (केवल repository clone करने के लिए), hard subtitles जोड़ने हेतु `subtitles` (libass) फ़िल्टर वाला FFmpeg/FFprobe और चालू Redis सेवा चाहिए। `uv sync` Python libraries इंस्टॉल करता है, जिसमें Redis client (`redis>=5`) शामिल है; यह Redis server को इंस्टॉल या शुरू नहीं करता। यदि आपके पास पहले से remote Redis है, तो `config.local.json` में `redis.url` पर उसका पता सेट करें और दूसरा server इंस्टॉल न करें। Go 1.23 या नया संस्करण केवल Google Drive sidecar के लिए चाहिए; Node.js केवल frontend tests के लिए चाहिए। ये वैकल्पिक सुविधाएँ शुरू करने के लिए ज़रूरी नहीं हैं; केवल वही इंस्टॉल करें जिनका उपयोग करना है (एक ही कमांड में कई विकल्प जोड़ सकते हैं):

- `uv sync --extra local-translation`: स्थानीय अनुवाद, डिफ़ॉल्ट रूप से अंग्रेज़ी से चीनी।
- `uv sync --extra hf-models`: Hugging Face Whisper models।
- `uv sync --extra tts`: Kokoro dubbing; सिस्टम पर `espeak-ng` भी इंस्टॉल करें ([TTS](../tts.md) देखें)।

Python packages इंस्टॉल करने से model weights शामिल नहीं होते। पहला task शुरू करने से पहले Settings में स्थानीय अनुवाद और Whisper models डाउनलोड या import करें और उनकी readiness जाँचें। Kokoro पहली बार voice generate करते समय weights डाउनलोड करता है। Voice separation चालू करने पर Demucs अलग से इंस्टॉल करें।

### macOS पर बिल्ड

macOS पर प्रोजेक्ट को स्थानीय रूप से चलाने के लिए Python 3.10–3.12, `uv` और libass support वाला FFmpeg चाहिए। API वेब वर्कबेंच भी उपलब्ध कराता है, इसलिए Node.js अलग से इंस्टॉल करने की आवश्यकता नहीं है।

किसी उपयुक्त डायरेक्टरी में ये कमांड चलाएँ:

```sh
git clone https://github.com/S-zhi/TranslatedSubs.git
cd TranslatedSubs
brew install uv redis
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
uv sync
cp config.example.json config.local.json
brew services start redis
redis-cli ping
```

आख़िरी कमांड का उत्तर `PONG` होना चाहिए। API शुरू करने से पहले इसे चलाएँ।

सेवा शुरू होने के बाद Settings या API में मॉडल क्रेडेंशियल और व्यवसाय सेटिंग कॉन्फ़िगर करें। ये Redis में सहेजे जाते हैं और पुनः शुरू किए बिना लागू होते हैं.

API और वेब वर्कबेंच शुरू करें:

```sh
uv run python -m src.cli
```

<http://127.0.0.1:8000/> खोलें। सेवा की स्थिति और FFmpeg में हार्ड-सबटाइटल फ़िल्टर की उपलब्धता जाँचें:

```sh
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
ffmpeg -hide_banner -filters | grep " subtitles "
```

### Windows पर बिल्ड

Windows 10 या 11 पर PowerShell से TranslatedSubs को सोर्स कोड से चला सकते हैं। प्रोजेक्ट Python 3.10–3.12 का समर्थन करता है; इन चरणों में Python 3.12 और `uv.lock` में लॉक किए गए संस्करण इस्तेमाल होते हैं। API वेब वर्कबेंच देता है, इसलिए Node.js अलग से इंस्टॉल करने की ज़रूरत नहीं है।

1. `uv` इंस्टॉल करें:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   इंस्टॉलेशन के बाद PowerShell फिर से खोलें।

2. FFmpeg इंस्टॉल करें। [FFmpeg डाउनलोड पेज](https://ffmpeg.org/download.html) पर Windows build चुनें और Gyan का full build डाउनलोड करें। इसे extract करें और इसके `bin` डायरेक्टरी (उदाहरण के लिए, `C:\ffmpeg\bin`) को `PATH` में जोड़ें; फिर PowerShell फिर से खोलें। जाँचें कि `ffmpeg`, `ffprobe` और हार्ड-सबटाइटल फ़िल्टर उपलब्ध हैं:

   ```powershell
   ffprobe -version
   ffmpeg -hide_banner -filters | findstr /i subtitles
   ```

3. प्रोजेक्ट clone करें, निर्भरताएँ इंस्टॉल करें और स्थानीय कॉन्फ़िगरेशन बनाएँ:

   ```powershell
   git clone https://github.com/S-zhi/TranslatedSubs.git
   cd TranslatedSubs
   uv python install 3.12
   uv sync --python 3.12 --locked
   Copy-Item config.example.json config.local.json
   ```

4. Linux containers के लिए सेट किए गए Docker Desktop से Redis तैयार करें। यदि आपके पास पहले से remote Redis है, तो `config.local.json` में `redis.url` पर उसका पता सेट करके इस चरण को छोड़ सकते हैं।

   ```powershell
   docker run -d --name translatedsubs-redis --restart unless-stopped -p 127.0.0.1:6379:6379 -v translatedsubs-redis-data:/data redis:7-alpine redis-server --appendonly yes
   docker exec translatedsubs-redis redis-cli ping
   ```

   दूसरी कमांड का उत्तर `PONG` होना चाहिए।

   सेवा शुरू होने के बाद Settings या API में मॉडल क्रेडेंशियल और व्यवसाय सेटिंग कॉन्फ़िगर करें। ये Redis में सहेजे जाते हैं और पुनः शुरू किए बिना लागू होते हैं.

API और वेब वर्कबेंच शुरू करें:

```powershell
uv run --locked python -m src.cli --host 127.0.0.1 --port 8000
```

<http://127.0.0.1:8000/> खोलें। सेवा की स्थिति जाँचने के लिए:

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Docker से तुरंत शुरू करें

रिपॉज़िटरी की मूल डायरेक्टरी में ये कमांड चलाएँ:

```bash
cp config.example.json config.local.json
```

`config.local.json` संपादित करके API को Compose Redis सेवा और `/data` paths के लिए सेट करें:

```json
{
  "redis": {
    "url": "redis://redis:6379/0",
    "namespace": "translatedsubs-local"
  },
  "paths": {
    "dataDir": "/data",
    "dbPath": "/data/db/app.db"
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8000
  }
}
```

फिर services शुरू करें:

```bash
# डिफ़ॉल्ट स्थानीय अनुवादक को API कुंजी की आवश्यकता नहीं है; क्लाउड कुंजी केवल आवश्यकता होने पर सेट करें।
docker compose up -d --build
```

Compose API और Redis डाउनलोड करके शुरू करता है; host पर Redis, Python, `uv` या FFmpeg इंस्टॉल करने की ज़रूरत नहीं है। अपग्रेड के समय Redis और data persistent volumes सुरक्षित रखें। डिफ़ॉल्ट image में केवल आधारभूत dependencies हैं; optional extras इस्तेमाल करने के लिए ज़रूरी extras और system packages को `Dockerfile` में जोड़कर image फिर से बनाएँ। Model weights अलग से डाउनलोड होते हैं। पुराने `.env` संस्करण से आने पर API शुरू करने से पहले [.env से migration गाइड](../local-quick-start.md) का पालन करें।

<http://localhost:8000/> खोलें। API चल रही है या नहीं, यह जाँचने के लिए `curl http://127.0.0.1:8000/api/health` चलाएँ; सही उत्तर में `"ok":true` होगा। `/api/health/ready` अनुवाद कुंजी, FFmpeg, स्टोरेज और हार्ड सबटाइटल फ़िल्टर की स्थिति भी दिखाता है। स्थानीय विकास और Linux पर तैनाती के लिए [दस्तावेज़ सूची (चीनी में)](../README.md) देखें।

## क्षमताएँ

- **सबटाइटल प्रक्रिया**: वीडियो डाउनलोड करना, ऑडियो निकालना, वाणी का प्रतिलेखन और अनुवाद करना तथा अलग या वीडियो में स्थायी रूप से जुड़े सबटाइटल बनाना।
- **वेब कार्यक्षेत्र**: कार्य कतार और प्रगति देखना, वीडियो का पूर्वावलोकन करना, सबटाइटल संपादित करना और नतीजे डाउनलोड करना।
- **बहुभाषी इंटरफ़ेस**: साइडबार से सरल चीनी, अंग्रेज़ी, हिंदी, स्पेनिश, अरबी, फ़्रेंच, पुर्तगाली और रूसी के बीच स्विच करें। आपकी पसंद ब्राउज़र में सहेजी जाती है; पहली बार आने पर ब्राउज़र की भाषा इस्तेमाल होती है, जब तक डिप्लॉयमेंट में कोई डिफ़ॉल्ट भाषा तय न की गई हो। डिफ़ॉल्ट भाषा `web/config.js` में `UI_LOCALE` से तय करें।
- **MCP एकीकरण**: Codex, Claude Desktop और अन्य AI क्लाइंट स्वाभाविक भाषा से कार्य बना और उनकी प्रगति देख सकते हैं।
- **Google Drive विस्तार**: टीम के साथ नतीजे साझा करने के लिए हर कार्य की फ़ाइलें अपलोड, डाउनलोड और व्यवस्थित करना।

## वीडियो से सबटाइटल तक

1. वेब कार्यक्षेत्र में वीडियो पेज का लिंक डालें या स्थानीय वीडियो अपलोड करें। पहले डाउनलोड जाँच से लिंक की उपलब्धता देख सकते हैं।
2. स्रोत और लक्ष्य भाषा, केवल अनुवाद या द्विभाषी सबटाइटल, तथा अलग या वीडियो में स्थायी रूप से जुड़े सबटाइटल चुनें। प्रतिलेखन के लिए डिफ़ॉल्ट बैकएंड स्थानीय faster-whisper है। पहला काम शुरू करने से पहले स्थानीय मॉडल सेटिंग में चुना हुआ मॉडल डाउनलोड करें और उसके तैयार होने की प्रतीक्षा करें।
3. काम शुरू करके कतार में डाउनलोड, ऑडियो निकालने, प्रतिलेखन, अनुवाद और परिणाम तैयार होने की प्रगति देखें। पूरा होने पर वीडियो देखें, सबटाइटल संपादित करें, परिणाम दोबारा बनाएँ और वीडियो व SRT डाउनलोड करें। केवल वीडियो डाउनलोड मोड सबटाइटल नहीं बनाता।

अलग सबटाइटल को प्लेयर में चालू या बंद किया जा सकता है। वीडियो में स्थायी रूप से जुड़े सबटाइटल के लिए FFmpeg का `subtitles` (libass) फ़िल्टर चाहिए। पहली बार मॉडल डाउनलोड करने और बाहरी सेवाओं तक पहुँच की ज़रूरत पड़ सकती है। AI क्लाइंट इसी प्रक्रिया को [MCP Agent गाइड (चीनी में)](../mcp-agent-guide.md) से चला सकते हैं।

## मुख्य कॉन्फ़िगरेशन और डेटा

`config.example.json` को `config.local.json` में कॉपी करें और Redis शुरू करें। मॉडल क्रेडेंशियल और सेवा सेटिंग Settings या configuration API से बदलें। मान Redis में सहेजे जाते हैं और सेवा पुनः शुरू किए बिना नई रिक्वेस्ट पर लागू होते हैं।

Bootstrap फ़ाइल में मशीन के Redis, डेटा-पथ और listener सेटिंग हैं। अपग्रेड के समय डेटा और Redis volumes सुरक्षित रखें। `config.local.json`, `.env`, Cookie, OAuth क्रेडेंशियल या परीक्षण मीडिया को repository में न जोड़ें। Google Drive के लिए अलग sidecar चाहिए; [स्थानीय त्वरित शुरुआत (चीनी में)](../local-quick-start.md) देखें।

## आम समस्याएँ

- API जवाब देती है, लेकिन काम शुरू नहीं होता: `/api/health/ready` के `checks` और `capabilities` में कुंजी, FFmpeg/FFprobe, yt-dlp और स्टोरेज की स्थिति देखें।
- `MODEL_NOT_READY`: चुने हुए Whisper मॉडल को स्थानीय मॉडल सेटिंग में डाउनलोड और जाँचें।
- हार्ड सबटाइटल उपलब्ध नहीं हैं: libass वाला FFmpeg स्थापित करें या अलग सबटाइटल चुनें। फ़िल्टर की जाँच `ffmpeg -hide_banner -filters | grep ' subtitles '` से करें।
- लिंक डाउनलोड नहीं होता: पहले डाउनलोड जाँच चलाएँ; लॉगिन ज़रूरी हो तो [स्थानीय गाइड (चीनी में)](../local-quick-start.md) के अनुसार cookie file setting भरें।

## दस्तावेज़

नीचे की अधिकांश विस्तृत गाइड चीनी भाषा में हैं; प्रतिलेखन सेवा प्रोटोकॉल अंग्रेज़ी में है।

- [दस्तावेज़ सूची](../README.md): उपयोग के अनुसार तैनाती और विस्तार की गाइड।
- [स्थानीय त्वरित शुरुआत](../local-quick-start.md): macOS/Linux, पर्यावरण चर और Google Drive sidecar।
- [Linux पर तैनाती](../quick-start-linux.md): Ubuntu/Debian स्थापना, systemd, रिवर्स प्रॉक्सी और समस्या निवारण।
- [MCP Server](../mcp-server.md): stdio, Streamable HTTP और उपलब्ध टूल।
- [MCP Agent गाइड](../mcp-agent-guide.md): कॉल क्रम, कार्य स्थिति और त्रुटियाँ।
- [Google Drive sidecar](../../drive-service/README.md): क्लाउड फ़ाइल सिंक का API और कॉन्फ़िगरेशन।

## विकास

यह परियोजना Python 3.10–3.12, FastAPI, FFmpeg और साधारण JavaScript का उपयोग करती है। स्थानीय विकास के लिए `uv sync` और फिर `uv run python -m src.cli` चलाएँ; यही सेवा वेब इंटरफ़ेस भी उपलब्ध कराती है। Python जाँच `uv run pytest -q` से और फ्रंटएंड जाँच `web/` में `npm test` से करें। वास्तविक क्लाउड सेवाओं वाली जाँच अलग से सक्रिय करनी होती है; [AGENTS.md (चीनी में)](../../AGENTS.md) देखें।

`src/handler/` HTTP API देता है; `src/core/` डाउनलोड, प्रतिलेखन और सबटाइटल संभालता है; `src/service/` और `src/store/` काम तथा डेटा रखते हैं; `src/mcp_server/` MCP जोड़ता है; `web/` ब्राउज़र कार्यक्षेत्र है। विकास नियम [CONTRIBUTING.md (चीनी में)](../../.github/CONTRIBUTING.md) में हैं। सुरक्षा समस्या [SECURITY.md](../../.github/SECURITY.md) के अनुसार निजी तौर पर बताएँ, सार्वजनिक Issue में नहीं।

## लाइसेंस और अनुपालन

यह परियोजना [MIT License](../../LICENSE) के अंतर्गत जारी की गई है। केवल वही सामग्री संसाधित करें जिसे देखने, डाउनलोड करने, प्रतिलेखित करने, अनुवाद करने और पुनः वितरित करने की आपको अनुमति है। मूल वेबसाइट की शर्तों, कॉपीराइट प्रतिबंधों और लागू कानूनों का पालन करें।
