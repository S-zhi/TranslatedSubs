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

## Docker से तुरंत शुरू करें

रिपॉज़िटरी की मूल डायरेक्टरी में ये कमांड चलाएँ:

```bash
cp .env.example .env
# Set SUBTRANS_DEEPSEEK_API_KEY in .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

मौजूदा कंटेनर को अपग्रेड करते समय `translatedsubs-data` की जगह वर्तमान वॉल्यूम का नाम दें, ताकि कार्य डेटाबेस और तैयार फ़ाइलें सुरक्षित रहें। मौजूदा `SUBTRANS_*` पर्यावरण चर समर्थित हैं।

<http://localhost:8000/> खोलें। API चल रही है या नहीं, यह जाँचने के लिए `curl http://127.0.0.1:8000/api/health` चलाएँ; सही उत्तर में `"ok":true` होगा। `/api/health/ready` अनुवाद कुंजी, FFmpeg, स्टोरेज और हार्ड सबटाइटल फ़िल्टर की स्थिति भी दिखाता है। स्थानीय विकास और Linux पर तैनाती के लिए [दस्तावेज़ सूची (चीनी में)](../README.md) देखें।

## क्षमताएँ

- **सबटाइटल प्रक्रिया**: वीडियो डाउनलोड करना, ऑडियो निकालना, वाणी का प्रतिलेखन और अनुवाद करना तथा अलग या वीडियो में स्थायी रूप से जुड़े सबटाइटल बनाना।
- **वेब कार्यक्षेत्र**: कार्य कतार और प्रगति देखना, वीडियो का पूर्वावलोकन करना, सबटाइटल संपादित करना और नतीजे डाउनलोड करना।
- **MCP एकीकरण**: Codex, Claude Desktop और अन्य AI क्लाइंट स्वाभाविक भाषा से कार्य बना और उनकी प्रगति देख सकते हैं।
- **Google Drive विस्तार**: टीम के साथ नतीजे साझा करने के लिए हर कार्य की फ़ाइलें अपलोड, डाउनलोड और व्यवस्थित करना।
- **बदले जा सकने वाले प्रतिलेखन बैकएंड**: लागत, गति और गोपनीयता के अनुसार स्थानीय faster-whisper, Replicate या संगत HTTP सेवा चुनना।

## वीडियो से सबटाइटल तक

1. वेब कार्यक्षेत्र में वीडियो पेज का लिंक डालें या स्थानीय वीडियो अपलोड करें। पहले डाउनलोड जाँच से लिंक की उपलब्धता देख सकते हैं।
2. स्रोत और लक्ष्य भाषा, केवल अनुवाद या द्विभाषी सबटाइटल, तथा अलग या वीडियो में स्थायी रूप से जुड़े सबटाइटल चुनें। प्रतिलेखन के लिए डिफ़ॉल्ट बैकएंड स्थानीय faster-whisper है। पहला काम शुरू करने से पहले स्थानीय मॉडल सेटिंग में चुना हुआ मॉडल डाउनलोड करें और उसके तैयार होने की प्रतीक्षा करें।
3. काम शुरू करके कतार में डाउनलोड, ऑडियो निकालने, प्रतिलेखन, अनुवाद और परिणाम तैयार होने की प्रगति देखें। पूरा होने पर वीडियो देखें, सबटाइटल संपादित करें, परिणाम दोबारा बनाएँ और वीडियो व SRT डाउनलोड करें। केवल वीडियो डाउनलोड मोड सबटाइटल नहीं बनाता।

अलग सबटाइटल को प्लेयर में चालू या बंद किया जा सकता है। वीडियो में स्थायी रूप से जुड़े सबटाइटल के लिए FFmpeg का `subtitles` (libass) फ़िल्टर चाहिए। पहली बार मॉडल डाउनलोड करने और बाहरी सेवाओं तक पहुँच की ज़रूरत पड़ सकती है। AI क्लाइंट इसी प्रक्रिया को [MCP Agent गाइड (चीनी में)](../mcp-agent-guide.md) से चला सकते हैं।

## मुख्य कॉन्फ़िगरेशन और डेटा

`.env.example` की प्रति बनाएँ और `.env` में `SUBTRANS_DEEPSEEK_API_KEY` भरें। सभी सेटिंग और उनके डिफ़ॉल्ट मान [पर्यावरण चर टेम्पलेट](../../.env.example) में हैं। सामान्य सेटिंग ये हैं:

| सेटिंग | उपयोग |
| --- | --- |
| `SUBTRANS_DEEPSEEK_API_KEY` | सबटाइटल अनुवाद के लिए DeepSeek कुंजी; इसके बिना पूरी प्रक्रिया तैयार नहीं होगी। |
| `SUBTRANS_DATA_DIR`, `SUBTRANS_DB` | फ़ाइलों और SQLite कार्य डेटाबेस का स्थान; Docker उदाहरण दोनों को स्थायी वॉल्यूम में रखता है। |
| `SUBTRANS_TRANSCRIBER_BACKEND` | डिफ़ॉल्ट `local_whisper`; आवश्यकता पर `replicate` या संगत HTTP सेवा चुनें। |
| `SUBTRANS_COOKIES` | उन वेबसाइटों के लिए Cookie फ़ाइल जो लॉगिन या उम्र सत्यापन माँगती हैं। |
| `SUBTRANS_WORKERS`, `SUBTRANS_DOWNLOAD_WORKERS` | प्रक्रिया और डाउनलोड के समानांतर चलने वाले कामों की सीमा। |

कंटेनर अपग्रेड करते समय मौजूदा वॉल्यूम दोबारा उपयोग करें और परिणामों के साथ SQLite डेटाबेस भी सुरक्षित रखें। `.env`, Cookie, OAuth क्रेडेंशियल या जाँच के दौरान बनी मीडिया फ़ाइलें रिपॉज़िटरी में जमा न करें। Google Drive के लिए अलग sidecar चाहिए; [स्थानीय त्वरित शुरुआत (चीनी में)](../local-quick-start.md) देखें।

## आम समस्याएँ

- API जवाब देती है, लेकिन काम शुरू नहीं होता: `/api/health/ready` के `checks` और `capabilities` में कुंजी, FFmpeg/FFprobe, yt-dlp और स्टोरेज की स्थिति देखें।
- `MODEL_NOT_READY`: चुने हुए Whisper मॉडल को स्थानीय मॉडल सेटिंग में डाउनलोड और जाँचें।
- हार्ड सबटाइटल उपलब्ध नहीं हैं: libass वाला FFmpeg स्थापित करें या अलग सबटाइटल चुनें। फ़िल्टर की जाँच `ffmpeg -hide_banner -filters | grep ' subtitles '` से करें।
- लिंक डाउनलोड नहीं होता: पहले डाउनलोड जाँच चलाएँ; लॉगिन ज़रूरी हो तो [स्थानीय गाइड (चीनी में)](../local-quick-start.md) के अनुसार `SUBTRANS_COOKIES` भरें।

## दस्तावेज़

नीचे की अधिकांश विस्तृत गाइड चीनी भाषा में हैं; प्रतिलेखन सेवा प्रोटोकॉल अंग्रेज़ी में है।

- [दस्तावेज़ सूची](../README.md): उपयोग के अनुसार तैनाती और विस्तार की गाइड।
- [स्थानीय त्वरित शुरुआत](../local-quick-start.md): macOS/Linux, पर्यावरण चर और Google Drive sidecar।
- [Linux पर तैनाती](../quick-start-linux.md): Ubuntu/Debian स्थापना, systemd, रिवर्स प्रॉक्सी और समस्या निवारण।
- [MCP Server](../mcp-server.md): stdio, Streamable HTTP और उपलब्ध टूल।
- [MCP Agent गाइड](../mcp-agent-guide.md): कॉल क्रम, कार्य स्थिति और त्रुटियाँ।
- [प्रतिलेखन सेवा प्रोटोकॉल (अंग्रेज़ी में)](../transcriber-service.md): स्थानीय, Replicate और HTTP बैकएंड।
- [Google Drive sidecar](../../drive-service/README.md): क्लाउड फ़ाइल सिंक का API और कॉन्फ़िगरेशन।

## विकास

यह परियोजना Python 3.10–3.12, FastAPI, FFmpeg और साधारण JavaScript का उपयोग करती है। स्थानीय विकास के लिए `uv sync` और फिर `uv run uvicorn src.handler.app:app --port 8000` चलाएँ; यही सेवा वेब इंटरफ़ेस भी उपलब्ध कराती है। Python जाँच `uv run pytest -q` से और फ्रंटएंड जाँच `web/` में `npm test` से करें। वास्तविक क्लाउड सेवाओं वाली जाँच अलग से सक्रिय करनी होती है; [AGENTS.md (चीनी में)](../../AGENTS.md) देखें।

`src/handler/` HTTP API देता है; `src/core/` डाउनलोड, प्रतिलेखन और सबटाइटल संभालता है; `src/service/` और `src/store/` काम तथा डेटा रखते हैं; `src/mcp_server/` MCP जोड़ता है; `web/` ब्राउज़र कार्यक्षेत्र है। विकास नियम [CONTRIBUTING.md (चीनी में)](../../.github/CONTRIBUTING.md) में हैं। सुरक्षा समस्या [SECURITY.md](../../.github/SECURITY.md) के अनुसार निजी तौर पर बताएँ, सार्वजनिक Issue में नहीं।

## लाइसेंस और अनुपालन

यह परियोजना [MIT License](../../LICENSE) के अंतर्गत जारी की गई है। केवल वही सामग्री संसाधित करें जिसे देखने, डाउनलोड करने, प्रतिलेखित करने, अनुवाद करने और पुनः वितरित करने की आपको अनुमति है। मूल वेबसाइट की शर्तों, कॉपीराइट प्रतिबंधों और लागू कानूनों का पालन करें।
