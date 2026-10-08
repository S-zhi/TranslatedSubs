[English](../../README.md) | [简体中文](../zh/README.md) | [हिन्दी](../hi/README.md) | [Español](../es/README.md) | [Français](../fr/README.md) | Português | [Русский](../ru/README.md)

<div align="center">
  <img src="../assets/eye-subtitles-logo.svg" width="88" alt="Logotipo do TranslatedSubs" />
  <h1>TranslatedSubs</h1>
  <p><strong>Compreenda tanto a fala quanto o texto na tela: transcrição, tradução de legendas e reconhecimento de conteúdo escrito em quadros.</strong></p>
  <p>Implantação rápida com integração MCP.</p>
</div>

TranslatedSubs é um ambiente de trabalho para compreensão de vídeo e áudio: baixa mídias, transcreve falas, traduz legendas e reconhece conteúdo escrito exibido nos vídeos.

- **Vídeos de várias fontes**: baixe diretamente das plataformas compatíveis, sem precisar baixar e enviar os arquivos novamente.
- **Implantação local e modelos flexíveis**: inicie rapidamente ou use uma configuração leve; configure modelos mais avançados para melhorar a correspondência, incluindo modelos da comunidade.
- **Tarefas paralelas e recursos visíveis**: execute vários trabalhos ao mesmo tempo e acompanhe o status e o uso de recursos. A sincronização com o Google Drive também está disponível.
- **Serviço dedicado de ajuste fino**: ajuste modelos compatíveis em um serviço separado para melhorar os resultados conforme suas necessidades.
- **Uso em vários dispositivos**: disponível na web, Windows e macOS; o suporte a celulares está planejado para o futuro.

Plataformas de vídeo compatíveis:
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

## Compilar a partir do código-fonte

### Build no macOS

Para executar o projeto localmente no macOS, você precisa do Python 3.10–3.12, do `uv` e do FFmpeg com suporte a libass. A API também serve a interface web, então não é necessário instalar o Node.js separadamente.

Execute estes comandos em um diretório adequado:

```sh
git clone https://github.com/S-zhi/TranslatedSubs.git
cd TranslatedSubs
brew install uv
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
uv sync
cp .env.example .env
```

Você pode adicionar ao `.env` as chaves de API na nuvem necessárias ou configurá-las depois de iniciar o serviço, que é a opção recomendada.

Inicie a API e a interface web:

```sh
uv run uvicorn src.handler.app:app --port 8000
```

Abra <http://127.0.0.1:8000/>. Verifique o serviço e confirme que o FFmpeg tem o filtro de legendas embutidas:

```sh
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
ffmpeg -hide_banner -filters | grep " subtitles "
```

### Build no Windows

No Windows 10 ou 11, você pode executar o TranslatedSubs a partir do código-fonte pelo PowerShell. O projeto aceita Python 3.10–3.12; estas etapas usam o Python 3.12 e instalam as versões fixadas em `uv.lock`. A API serve a interface web, sem necessidade de instalar o Node.js separadamente.

1. Instale o `uv`:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   Abra o PowerShell novamente após a instalação.

2. Instale o FFmpeg. Na [página de downloads do FFmpeg](https://ffmpeg.org/download.html), escolha uma versão para Windows e baixe a versão completa do Gyan. Extraia-a e adicione a pasta `bin` (por exemplo, `C:\ffmpeg\bin`) ao `PATH`; depois, abra o PowerShell novamente. Verifique se `ffmpeg`, `ffprobe` e o filtro de legendas embutidas estão disponíveis:

   ```powershell
   ffprobe -version
   ffmpeg -hide_banner -filters | findstr /i subtitles
   ```

3. Clone o projeto, instale as dependências e crie a configuração local:

   ```powershell
   git clone https://github.com/S-zhi/TranslatedSubs.git
   cd TranslatedSubs
   uv python install 3.12
   uv sync --python 3.12 --locked
   Copy-Item .env.example .env
   ```

   Você pode adicionar ao `.env` as chaves de API na nuvem necessárias ou configurá-las depois de iniciar o serviço, que é a opção recomendada.

Inicie a API e a interface web:

```powershell
uv run --locked uvicorn src.handler.app:app --host 127.0.0.1 --port 8000
```

Abra <http://127.0.0.1:8000/>. Verifique o serviço com:

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Início rápido com Docker

Execute os comandos na raiz do repositório:

```bash
cp .env.example .env
# Defina SUBTRANS_DEEPSEEK_API_KEY em .env
docker build -t translatedsubs:local . && docker run -d --name translatedsubs --restart unless-stopped -p 8000:8000 --env-file .env -e SUBTRANS_DATA_DIR=/data -e SUBTRANS_DB=/data/db/app.db -v translatedsubs-data:/data translatedsubs:local
```

Ao atualizar um contêiner existente, substitua `translatedsubs-data` pelo nome do volume atual para preservar o banco de dados de tarefas e os arquivos gerados. As variáveis de ambiente `SUBTRANS_*` continuam disponíveis.

Acesse <http://localhost:8000/>. Execute `curl http://127.0.0.1:8000/api/health` para confirmar que a API está ativa; a resposta normal contém `"ok":true`. `/api/health/ready` também informa o estado da chave de tradução, do FFmpeg, do armazenamento e do filtro de legendas gravadas. Para desenvolvimento local e implantação no Linux, consulte o [índice da documentação (em chinês)](../README.md).

## Recursos

- **Fluxo de legendagem**: Baixa vídeos, extrai áudio, transcreve e traduz a fala e gera arquivos de legenda ou vídeos com legendas incorporadas.
- **Interface web**: Gerencia a fila de tarefas, acompanha o progresso, permite visualizar vídeos, editar legendas e baixar resultados no navegador.
- **Interface multilíngue**: Alterne pela barra lateral entre chinês simplificado, inglês, hindi, espanhol, árabe, francês, português e russo. A escolha fica salva no navegador; na primeira visita, usa-se o idioma do navegador, a menos que a implantação tenha definido um idioma padrão. Defina esse padrão com `UI_LOCALE` em `web/config.js`.
- **Integração MCP**: Permite que Codex, Claude Desktop e outros clientes de IA criem e acompanhem tarefas por linguagem natural.
- **Extensão Google Drive**: Envia, baixa e organiza arquivos por tarefa para compartilhar resultados com a equipe.
- **Mecanismos de transcrição substituíveis**: Escolha entre faster-whisper local, Replicate ou um serviço HTTP compatível conforme custo, velocidade e privacidade.

## Do vídeo às legendas

1. Cole o endereço de uma página de vídeo na interface web ou envie um vídeo local. Você pode usar o teste de download para verificar o endereço antes.
2. Escolha os idiomas de origem e destino, legendas apenas traduzidas ou bilíngues e legendas separadas ou gravadas no vídeo. O backend padrão de transcrição é o faster-whisper local. Antes da primeira tarefa, baixe o modelo escolhido nas configurações de modelos locais e aguarde até que fique pronto.
3. Envie a tarefa e acompanhe o download, a extração do áudio, a transcrição, a tradução e a montagem na fila. Ao concluir, visualize o vídeo, edite as legendas, gere o resultado novamente e baixe o vídeo e o SRT. O modo de apenas download não gera legendas.

Legendas separadas podem ser ativadas ou desativadas no player. Legendas gravadas fazem parte da imagem e exigem o filtro `subtitles` (libass) do FFmpeg. A primeira execução pode precisar baixar um modelo e acessar serviços externos. Clientes de IA podem usar o mesmo fluxo pelo [guia de agentes MCP (em chinês)](../mcp-agent-guide.md).

## Configuração e dados

Copie `.env.example` e preencha `SUBTRANS_DEEPSEEK_API_KEY` em `.env`. O [modelo de variáveis de ambiente](../../.env.example) lista todos os ajustes e valores padrão. Os mais usados são:

| Ajuste | Finalidade |
| --- | --- |
| `SUBTRANS_DEEPSEEK_API_KEY` | Chave da DeepSeek para traduzir legendas; sem ela, o fluxo completo não fica pronto. |
| `SUBTRANS_DATA_DIR`, `SUBTRANS_DB` | Locais dos arquivos e do banco SQLite de tarefas; o exemplo Docker guarda ambos em um volume persistente. |
| `SUBTRANS_TRANSCRIBER_BACKEND` | O padrão é `local_whisper`; selecione `replicate` ou um serviço HTTP compatível quando necessário. |
| `SUBTRANS_COOKIES` | Arquivo de cookies para sites que exigem login ou verificação de idade. |
| `SUBTRANS_WORKERS`, `SUBTRANS_DOWNLOAD_WORKERS` | Limites de concorrência do processamento e dos downloads. |

Reutilize o volume existente ao atualizar o contêiner e preserve o banco SQLite junto com os resultados. Não envie `.env`, cookies, credenciais OAuth ou mídia de testes ao repositório. Google Drive exige um sidecar separado; consulte o [início rápido local (em chinês)](../local-quick-start.md).

## Problemas comuns

- A API responde, mas as tarefas não começam: examine `checks` e `capabilities` em `/api/health/ready` para verificar a chave, FFmpeg/FFprobe, yt-dlp e armazenamento.
- `MODEL_NOT_READY`: baixe e verifique o modelo Whisper selecionado nas configurações de modelos locais.
- Não é possível gravar legendas no vídeo: instale FFmpeg com libass ou selecione legendas separadas. Verifique o filtro com `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- O download do endereço falha: use primeiro o teste de download; se o site pedir login, configure `SUBTRANS_COOKIES` conforme o [guia local (em chinês)](../local-quick-start.md).

## Documentação

A maioria dos guias abaixo está em chinês; o protocolo do serviço de transcrição está em inglês.

- [Índice da documentação](../README.md): guias de implantação e extensões por caso de uso.
- [Início rápido local](../local-quick-start.md): macOS/Linux, variáveis de ambiente e Google Drive sidecar.
- [Implantação no Linux](../quick-start-linux.md): instalação no Ubuntu/Debian, systemd, proxy reverso e solução de problemas.
- [Servidor MCP](../mcp-server.md): stdio, Streamable HTTP e ferramentas.
- [Guia para agentes MCP](../mcp-agent-guide.md): sequência de chamadas, estados e erros.
- [Protocolo do serviço de transcrição (em inglês)](../transcriber-service.md): mecanismos local, Replicate e HTTP.
- [Google Drive sidecar](../../drive-service/README.md): API e configuração da sincronização de arquivos.

## Desenvolvimento

O projeto usa Python 3.10–3.12, FastAPI, FFmpeg e JavaScript nativo. Para desenvolver localmente, execute `uv sync` e depois `uv run uvicorn src.handler.app:app --port 8000`; o mesmo serviço hospeda a interface web. Execute os testes Python com `uv run pytest -q` e os testes do frontend com `npm test` em `web/`. Testes com serviços reais exigem ativação explícita; consulte [AGENTS.md (em chinês)](../../AGENTS.md).

`src/handler/` oferece a API HTTP; `src/core/` processa downloads, transcrição e legendas; `src/service/` e `src/store/` gerenciam tarefas e armazenamento; `src/mcp_server/` oferece MCP; e `web/` contém a interface. Consulte [CONTRIBUTING.md (em chinês)](../../.github/CONTRIBUTING.md). Comunique problemas de segurança de forma privada conforme [SECURITY.md](../../.github/SECURITY.md), nunca por uma issue pública.

## Licença e conformidade

O projeto é distribuído sob a [licença MIT](../../LICENSE). Processe apenas conteúdo que você tem autorização para acessar, baixar, transcrever, traduzir e redistribuir. Respeite os termos dos sites de origem, os direitos autorais e a legislação aplicável.
