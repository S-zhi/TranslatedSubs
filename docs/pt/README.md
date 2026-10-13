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

### Dependências necessárias

Para executar a partir do código-fonte, são necessários Python 3.10–3.12, `uv`, Git (somente para clonar o repositório), FFmpeg/FFprobe com o filtro `subtitles` (libass) para gravar legendas no vídeo e um serviço Redis em execução. `uv sync` instala as bibliotecas Python, incluindo o cliente Redis (`redis>=5`), mas não instala nem inicia o servidor Redis. Se já tiver um Redis remoto, configure o endereço em `redis.url` no `config.local.json` e não instale outro servidor. Go 1.23 ou posterior é necessário apenas para o sidecar do Google Drive; Node.js é usado apenas nos testes do frontend. Esses recursos opcionais não são necessários para iniciar; instale somente os que for usar (é possível combinar várias opções em um único comando):

- `uv sync --extra local-translation`: tradução local, de inglês para chinês por padrão.
- `uv sync --extra hf-models`: modelos Whisper do Hugging Face.
- `uv sync --extra tts`: dublagem com Kokoro; também exige `espeak-ng` instalado no sistema (consulte [TTS](../tts.md)).

A instalação dos pacotes Python não inclui os pesos dos modelos: antes da primeira tarefa, baixe ou importe em Configurações os modelos de tradução local e Whisper e verifique se estão prontos. O Kokoro baixa os pesos na primeira geração de voz. Instale o Demucs separadamente apenas se habilitar a separação de voz.

### Build no macOS

Para executar o projeto localmente no macOS, você precisa do Python 3.10–3.12, do `uv` e do FFmpeg com suporte a libass. A API também serve a interface web, então não é necessário instalar o Node.js separadamente.

Execute estes comandos em um diretório adequado:

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

O último comando deve responder `PONG`. Execute-o antes de iniciar a API.

Configure as credenciais dos modelos e as opções do serviço após iniciar, em Configurações ou pela API. Elas são salvas no Redis e aplicadas sem reiniciar.

Inicie a API e a interface web:

```sh
uv run python -m src.cli
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
   Copy-Item config.example.json config.local.json
   ```

4. Prepare o Redis com o Docker Desktop configurado para contêineres Linux. Se já tiver um Redis remoto, configure o endereço em `redis.url` no `config.local.json` e pule esta etapa.

   ```powershell
   docker run -d --name translatedsubs-redis --restart unless-stopped -p 127.0.0.1:6379:6379 -v translatedsubs-redis-data:/data redis:7-alpine redis-server --appendonly yes
   docker exec translatedsubs-redis redis-cli ping
   ```

   O segundo comando deve responder `PONG`.

   Configure as credenciais dos modelos e as opções do serviço após iniciar, em Configurações ou pela API. Elas são salvas no Redis e aplicadas sem reiniciar.

Inicie a API e a interface web:

```powershell
uv run --locked python -m src.cli --host 127.0.0.1 --port 8000
```

Abra <http://127.0.0.1:8000/>. Verifique o serviço com:

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Início rápido com Docker

Execute os comandos na raiz do repositório:

```bash
cp config.example.json config.local.json
```

Edite `config.local.json` para que a API use o serviço Redis do Compose e os caminhos `/data`:

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

Em seguida, inicie os serviços:

```bash
# O tradutor local padrão não precisa de uma chave de API; configure chaves de nuvem apenas quando necessário.
docker compose up -d --build
```

O Compose baixa e inicia a API e o Redis; não é necessário instalar Redis, Python, `uv` ou FFmpeg no host. Preserve os volumes persistentes de Redis e dados durante as atualizações. A imagem padrão contém apenas as dependências básicas; para usar extras opcionais, instale os extras e pacotes de sistema necessários no `Dockerfile` e reconstrua a imagem. Os pesos dos modelos são baixados separadamente. Para migrar de uma versão que usava `.env`, siga primeiro o guia de [migração a partir de .env](../local-quick-start.md), antes de iniciar a API.

Acesse <http://localhost:8000/>. Execute `curl http://127.0.0.1:8000/api/health` para confirmar que a API está ativa; a resposta normal contém `"ok":true`. `/api/health/ready` também informa o estado da chave de tradução, do FFmpeg, do armazenamento e do filtro de legendas gravadas. Para desenvolvimento local e implantação no Linux, consulte o [índice da documentação (em chinês)](../README.md).

## Recursos

- **Fluxo de legendagem**: Baixa vídeos, extrai áudio, transcreve e traduz a fala e gera arquivos de legenda ou vídeos com legendas incorporadas.
- **Interface web**: Gerencia a fila de tarefas, acompanha o progresso, permite visualizar vídeos, editar legendas e baixar resultados no navegador.
- **Interface multilíngue**: Alterne pela barra lateral entre chinês simplificado, inglês, hindi, espanhol, árabe, francês, português e russo. A escolha fica salva no navegador; na primeira visita, usa-se o idioma do navegador, a menos que a implantação tenha definido um idioma padrão. Defina esse padrão com `UI_LOCALE` em `web/config.js`.
- **Integração MCP**: Permite que Codex, Claude Desktop e outros clientes de IA criem e acompanhem tarefas por linguagem natural.
- **Extensão Google Drive**: Envia, baixa e organiza arquivos por tarefa para compartilhar resultados com a equipe.

## Do vídeo às legendas

1. Cole o endereço de uma página de vídeo na interface web ou envie um vídeo local. Você pode usar o teste de download para verificar o endereço antes.
2. Escolha os idiomas de origem e destino, legendas apenas traduzidas ou bilíngues e legendas separadas ou gravadas no vídeo. O backend padrão de transcrição é o faster-whisper local. Antes da primeira tarefa, baixe o modelo escolhido nas configurações de modelos locais e aguarde até que fique pronto.
3. Envie a tarefa e acompanhe o download, a extração do áudio, a transcrição, a tradução e a montagem na fila. Ao concluir, visualize o vídeo, edite as legendas, gere o resultado novamente e baixe o vídeo e o SRT. O modo de apenas download não gera legendas.

Legendas separadas podem ser ativadas ou desativadas no player. Legendas gravadas fazem parte da imagem e exigem o filtro `subtitles` (libass) do FFmpeg. A primeira execução pode precisar baixar um modelo e acessar serviços externos. Clientes de IA podem usar o mesmo fluxo pelo [guia de agentes MCP (em chinês)](../mcp-agent-guide.md).

## Configuração e dados

Copie `config.example.json` para `config.local.json` e inicie o Redis. Configure as credenciais dos modelos e as opções do serviço em Configurações ou pela API de configuração. Os valores são salvos no Redis e aplicados às novas solicitações sem reiniciar o serviço.

O arquivo bootstrap guarda as opções locais de Redis, caminhos de dados e escuta. Preserve os volumes de dados e Redis durante atualizações. Não envie `config.local.json`, `.env`, cookies, credenciais OAuth nem mídias geradas por testes ao repositório. Google Drive exige um sidecar separado; consulte o [início rápido local (em chinês)](../local-quick-start.md).

## Problemas comuns

- A API responde, mas as tarefas não começam: examine `checks` e `capabilities` em `/api/health/ready` para verificar a chave, FFmpeg/FFprobe, yt-dlp e armazenamento.
- `MODEL_NOT_READY`: baixe e verifique o modelo Whisper selecionado nas configurações de modelos locais.
- Não é possível gravar legendas no vídeo: instale FFmpeg com libass ou selecione legendas separadas. Verifique o filtro com `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- O download do endereço falha: use primeiro o teste de download; se o site pedir login, configure cookie file setting conforme o [guia local (em chinês)](../local-quick-start.md).

## Documentação

A maioria dos guias abaixo está em chinês; o protocolo do serviço de transcrição está em inglês.

- [Índice da documentação](../README.md): guias de implantação e extensões por caso de uso.
- [Início rápido local](../local-quick-start.md): macOS/Linux, variáveis de ambiente e Google Drive sidecar.
- [Implantação no Linux](../quick-start-linux.md): instalação no Ubuntu/Debian, systemd, proxy reverso e solução de problemas.
- [Servidor MCP](../mcp-server.md): stdio, Streamable HTTP e ferramentas.
- [Guia para agentes MCP](../mcp-agent-guide.md): sequência de chamadas, estados e erros.
- [Google Drive sidecar](../../drive-service/README.md): API e configuração da sincronização de arquivos.

## Desenvolvimento

O projeto usa Python 3.10–3.12, FastAPI, FFmpeg e JavaScript nativo. Para desenvolver localmente, execute `uv sync` e depois `uv run python -m src.cli`; o mesmo serviço hospeda a interface web. Execute os testes Python com `uv run pytest -q` e os testes do frontend com `npm test` em `web/`. Testes com serviços reais exigem ativação explícita; consulte [AGENTS.md (em chinês)](../../AGENTS.md).

`src/handler/` oferece a API HTTP; `src/core/` processa downloads, transcrição e legendas; `src/service/` e `src/store/` gerenciam tarefas e armazenamento; `src/mcp_server/` oferece MCP; e `web/` contém a interface. Consulte [CONTRIBUTING.md (em chinês)](../../.github/CONTRIBUTING.md). Comunique problemas de segurança de forma privada conforme [SECURITY.md](../../.github/SECURITY.md), nunca por uma issue pública.

## Licença e conformidade

O projeto é distribuído sob a [licença MIT](../../LICENSE). Processe apenas conteúdo que você tem autorização para acessar, baixar, transcrever, traduzir e redistribuir. Respeite os termos dos sites de origem, os direitos autorais e a legislação aplicável.
