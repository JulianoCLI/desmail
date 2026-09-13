# Desmail 2.3.0

Python 3.14 validado; dependencias diretas fixadas em `requirements.txt`.
Node 18+ para `client.js` e testes JavaScript.

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --host 127.0.0.1 --port 8000 --workers 1
```

## Contrato

- `GET /` e OpenAPI informam versao `2.3.0`.
- `POST /email/create`: JSON `{provider, domain, server, auto_confirm:true, expected_hosts:null, timeout_seconds:600}`; retorna `{session_id, email, provider, domain}`. Campos novos opcionais. `timeout_seconds` inteiro entre 1 e 3600; booleanos estritos. `expected_hosts`: null ou 1–20 dominios ASCII/punycode exatos, normalizados para minusculas; rejeita URLs, IPs, portas, wildcard, localhost e labels invalidos. Subdominio nao equivale ao dominio pai.
- `GET /email/status?session_id=...`: somente estado/cache, sem driver, navegacao ou consulta externa. Retorna `state`, `monitoring`, `auto_confirm`, `expected_hosts`, `timeout_seconds`, `expires_at`, `opened`, `verified`, `error`, `messages`, `inbox_state`. Sessao ausente: HTTP 404; entrada invalida: HTTP 422.
- Estados de status: `waiting_email`, `reading_body`, `opening_link`, `opened`, `verified`, `ambiguous`, `unknown`, `expired`, `disconnected`. `opened` preserva compatibilidade e encerra monitoramento, mas nao comprova verificacao; consulte `verified` e `confirmation.reason`.
- `GET /email/check`, `POST /email/poll` e `POST /email/sweep`: mensagens em `messages`, nao links. Estados: `ok`, `empty`, `missing`, `error`, `disconnected`, `in_progress`; `stale` indica cache antigo. Poll ocupado pode retornar `skipped`.
- `GET /email/body?session_id=...&mid=...`: `best`, `links`, `body_snippet`, `error`. Driver ocupado sem cache retorna `state=in_progress`; repetir depois. Erros de corpo nao entram no cache.
- `POST /email/open`: JSON `{session_id, mid, wait_s:6, revalidate:false}`. Usa mesmo driver, cache e tentativas do automatico. Mensagem precisa existir no inbox conhecido. `opened` nao significa `verified`.
- `revalidate:true` permite tentativa manual controlada, respeitando cooldown de 30s e limite de 3 tentativas. Navegacao incerta, inclusive timeout, nunca repete automaticamente.
- `verified:true` exige texto visivel exato `E-mail confirmado com sucesso. Sua conta já está liberada.` em `https://pokepixel.nietore.com` (443), normalizando somente espacos. Texto generico, oculto, origem diferente, HTTP 200 ou abertura nao bastam. Evidencia DOM, nao consulta independente ao backend da conta.
- QP somente com metadado explicito `encoding=quoted-printable`; resposta sem encoding preserva corpo literal. Integracao desse metadado no provedor nao foi comprovada.

## Links de confirmacao genericos

- Pokepixel continua aceito; dominio nao exclusivo. `expected_hosts` restringe candidatos antes da escolha; varios candidatos permitidos continuam ambiguos. Sem lista, qualquer candidato publico unico pode ser escolhido.
- Corpos sao lidos pelo cache existente mesmo sem palavra-chave no assunto, no maximo uma mensagem por sessao/lote. Assunto ou texto com “Confirme” nao autoriza primeiro HTTP arbitrario: exige evidencia no rotulo da ancora ou caminho/query (`confirme`, `confirmar`, `confirmacao`, `confirm`, `verify`, `verification`, `activate`; sem distinguir caixa ou acentos).
- Somente HTTP/HTTPS; rejeita userinfo, localhost, IPs nao globais e formas IPv4 alternativas. Homepage sem query, logo, unsubscribe, reset, login e payment sao excluidos mesmo com palavra de confirmacao.
- Um candidato unico pode abrir pelo fluxo auto-confirm existente. Varios candidatos distintos sao tratados conservadoramente como empate: `state=ambiguous`, `opened=false`, `candidates` com URLs exatas. Automatico nao repete mensagem ambigua. Consulte `GET /email/body` para revisar/copiar candidato e escolher manualmente fora do aplicativo; Enter/revalidacao nao escolhe primeiro nem abre todos.
- `GET /email/body` continua somente leitura; acrescenta `state` e `candidates`. `best` fica vazio no empate. Tokens preservam hifen, underscore e percent encoding; logs ocultam caminho, query e fragmento. Corpo e instrucoes nele sao somente dados.
- Parser reconhece HTTP/HTTPS, mas abertura exige **HTTPS porta 443**. Preflight DNS rejeita qualquer endereco nao global. Navegacao leve executa na aba criada com JavaScript habilitado, permitindo redirects, subresources e pagina dinamica; somente a aba criada e fechada em `finally`. Falha aborta; nenhum HTTP GET alternativo consome token.
- **Limites de seguranca:** DNS preflight nao fixa IP usado pelo Chrome; existe janela TOCTOU/DNS rebinding. Use filtro de egress que bloqueie redes privadas/reservadas para garantia de rede. Aba compartilha perfil/cookies do Chrome do projeto, nao representa contexto incognito. API sem autenticacao: manter bind em loopback.
- Observacao DOM inicia imediatamente apos navegacao, limitada a 14.5s (timeout de script 15s). `wait_s` (0-30s) continua minimo apos navegacao, incluindo observacao; nao soma espera fixa antes dela. `wait_s:0` nao desliga verificacao. Navegacao tem timeout 30s; DNS usa timeout do SO, portanto nao e limite total HTTP. Falha/timeout permanece incerta e nao repete automaticamente.

## API autonoma e integracao

Lifespan inicia uma unica thread de monitoramento por processo. Primeiro tick e a
cada 15s apos terminar tick anterior. Sem sessoes elegiveis, nenhum acesso ao
driver/provedor; startup nao cria Chrome. Create habilita monitoramento interno.
`auto_confirm:false` desliga monitoramento interno; operacoes manuais continuam
explicitas. Deadline monotonic impede novas leituras/aberturas automaticas e
confirmacoes manuais; operacao ja em voo termina e cache fica preservado.
`expired` nao apaga caixa. `opened`/`verified` preservam resultado mesmo apos prazo.
Shutdown aguarda worker e operacao sob lock antes de fechar Chrome proprio.

Obrigatorio **`--workers 1`**, sem reload em uso: sessoes/cache/guard vivem em memoria
e nao coordenam processos. TUI opcional: `python tui.py`. `iniciar.bat` inicia
somente API; `iniciar.bat tui` tambem abre TUI. Fechar TUI nao para monitoramento.

`client.js`: `createEmail(config)`, `getStatus(session_id)` e `releaseEmail(session_id)`
bastam; exemplo em `example.mjs`. Troque `expected_hosts` pelo dominio real antes de executar
`node example.mjs` (esse exemplo cria caixa externa). Cadastro/envio de mensagem
fica com integracao chamadora. Nao precisa chamar poll/sweep/autoconfirm.
Ciclo criar > usar > confirmar > apagar: `POST /email/release` remove a sessao da
memoria na hora e agenda a exclusao no provedor em fila assincrona (worker a cada
0.5s, sem bloquear create/poll); o proximo create nao espera essa limpeza.
`DELETE /email/{sid}` continua sincrono quando precisar de garantia imediata.
`GET /email/status` apos release retorna 404: sessao liberada.

### Diagnostico e decisoes DESHUB

`confirmation` aparece em status, `/email/open` (inclusive repeticao ignorada) e
resposta normal de `/email/autoconfirm` (tambem em `confirmed[]`). Antes de tentativa
de abertura vale `null`. Campos de tempo sao segundos Unix; `elapsed_ms` mede apenas
observacao DOM. `navigation_completed_at` nao existe se navegacao falhou;
`elapsed_ms` nao existe se observacao nao iniciou. `verified_at` so existe com valor
quando houve sucesso. `host` e host confiavel ou null, nunca origem arbitraria.

Razoes fixas: `success_visible`, `token_expired`, `token_used`,
`webgl_unsupported`, `unexpected_origin`, `success_not_observed`, `driver_error`,
`destination_blocked`. Negativas reconhecem elementos visiveis com texto exato:
`Token expirado.`, `Token já utilizado.`, `Token já foi utilizado.`,
`Your browser does not support WebGL`. Outras frases ficam `success_not_observed`
ao esgotar janela; nao inferir expiracao por idade do email.

Exemplo sintetico completo de `GET /email/status?session_id=demo1234`:

```json
{
  "session_id": "demo1234",
  "email": "offline@example.test",
  "state": "opened",
  "auto_confirm": true,
  "expected_hosts": ["pokepixel.nietore.com"],
  "timeout_seconds": 600,
  "monitoring": false,
  "opened": true,
  "verified": false,
  "confirmation": {
    "mid": "m1",
    "verified": false,
    "evidence": "",
    "verified_at": null,
    "reason": "success_not_observed",
    "started_at": 1800000001.0,
    "navigation_completed_at": 1800000001.0,
    "observed_at": 1800000015.5,
    "elapsed_ms": 14500,
    "host": "pokepixel.nietore.com"
  },
  "error": "",
  "messages": [{"mid": "m1"}],
  "inbox_state": "ok",
  "expires_at": 1800000600.0
}
```

1. Crie com `auto_confirm:true` e `expected_hosts:["pokepixel.nietore.com"]`.
   Guarde `session_id`; consulte `GET /email/status` a cada 2-5s.
2. Somente `verified === true` autoriza marcar verificacao concluida no HUB.
   `opened`, HTTP 200, `state:unknown`, deadline, `skipped` e 404 nao sao sucesso.
3. Se `verified !== true` e `monitoring === false`, pare polling de verificacao e
   mostre `confirmation.reason`; mantenha sessao para revisao. Nao repita token,
   nao use `revalidate:true` automaticamente e nao remova resultado incerto.
4. Salve diagnostico antes de liberar sessao apos sucesso. Para investigar remocao,
   consulte `GET /email/events?session_id=demo1234&limit=500`. Eventos `tab-verify`
   correlacionam `sid`/`mid`; `remove`, `release`, `auto-remove` retêm `confirmation`,
   `state`, `opened`, `verified`, `monitoring:false`, com timestamp de remocao em `t`.
   Buffer global de 500 eventos, somente neste processo; eventos antigos podem
   ser descartados. 404 posterior nao implica sucesso nem permite recuperar corpo.

Diagnosticos novos retêm somente codigos, evidencia fixa e tempos, sem corpo,
token ou email. Campos legados de resposta (`email`, `messages`, `link`,
`candidates`, `/email/body`) continuam sensiveis; HUB deve evitar logar resposta
inteira. Se `confirmation` nao existir em servidor antigo, tratar como desconhecido.

## TUI e coordenacao

Enter abre mensagem selecionada mesmo sem foco no inbox; identidade interna `(sid, mid)`.
`v` solicita revalidacao; `c` cria e copia, `n` repete configuracao, `y` copia email.
Sessoes mostram somente emails; coluna Conta mostra prefixo.
Auto-confirm ligado por padrao, polling 15s sem cancelar request em voo.
Confirmacao compartilha guard com worker, poll/sweep e abertura manual, inclusive
durante background apos resposta HTTP. Guard nao expira por idade e request em
voo nao e cancelado. API processa no maximo uma mensagem
por sessao em cada lote; libera lock entre sessoes. Driver ocupado retorna
progresso/cache para consultas e abertura manual; Enter pode ser repetido depois.
Cache e sessoes vivem em memoria: falha de consulta/driver preserva dados;
reinicio do processo e shutdown explicito nao persistem dados.

## Memoria e processos

Um unico Chrome headless serve todas as sessoes; sem ele nao ha create nem
leitura de corpo, porque o token `x-captcha` do smailpro so existe dentro da
pagina. Abrir navegador ja e opt-in: consultas usam `_driver_locked(create=False)`
e recusam criar navegador, entao a API ociosa fica com zero Chrome.

Tres mecanismos controlam o consumo:

- **Limpeza de orfaos no startup** (`reap_orphan_drivers`): encerra
  `uc_driver`/`chromedriver` e Chrome de automacao cujo processo pai ja morreu.
  Sem isso, cada encerramento forcado deixava driver vazando RAM e porta TCP.
  Nunca toca em processo com pai vivo nem no Chrome pessoal (identificado pela
  ausencia de `--remote-debugging-port`).
- **Flags de memoria** (`_MEM_FLAGS`): imagens/GPU/WebGL preservados; desliga
  extensoes e atividades de fundo. Desligar GPU impedia confirmacao Pokepixel.
- **Auto-shutdown por ociosidade** (opt-in): `DESMAIL_IDLE_SHUTDOWN_S=300` fecha
  o Chrome apos 5 min sem sessao; o proximo create reabre. Desligado por padrao
  (`0`), porque reabrir custa ~25 s no primeiro create.

## Icone na bandeja

```powershell
python tray.py           # inicia API + icone (iniciar.bat ja usa isto)
```

Icone verde na bandeja indica API no ar, com a contagem de caixas ativas no
tooltip. Encerrar pelo menu fecha API e todo Chrome relacionado.

A garantia de encerramento nao depende do Python: a API e lancada dentro de um
**Job Object do Windows** com `KILL_ON_JOB_CLOSE`. Se a bandeja morrer de
qualquer forma — inclusive Gerenciador de Tarefas, onde nenhum handler roda — o
proprio SO derruba a arvore inteira. Verificado: 11 processos ativos, kill
forcado na bandeja, 0 sobreviventes.

## Validacao offline

```powershell
python -m pytest -q -p no:cacheprovider
python local_browser_check.py
```

Ultima suite offline: **175 passed in 20.24s** (`--tb=short`). Nesta alteracao,
DOM executado em Node com ambiente simulado; Chrome/servicos reais nao executados.
Suite: lifecycle ASGI sem TUI, create/status, validacao, worker/manual, deadline,
DNS privado, timeout de navegacao, parser, Textual Pilot (Enter com/sem foco, evento antigo, log visivel),
cache, concorrencia manual/auto, polling lento, timeout, erros HTTP e JS real
executado em Node com fetch simulado. `local_browser_check.py` usa Chrome e
chromedriver ja instalados, perfil temporario e servidor localhost; cleanup em
`finally`, sem encerrar Chrome globalmente.

Chrome local preserva inbox, fecha aba criada e
mantem texto generico de sucesso sem verificacao. Teste substitui somente preflight
para localhost; producao continua rejeitando localhost. Integracao Smailpro/Sonjj/Pokepixel real,
criacao externa e consumo de tokens reais nao foram executados. Dependencias
transitivas nao possuem lockfile nem auditoria de vulnerabilidades nesta tarefa.
