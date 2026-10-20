# Automação de reset de senha XCash (CASI) + resposta no GLPI

Lê os chamados do XCash exportados do GLPI, reseta a senha no CASI e (opcional) responde o chamado.
Chamados incertos não são tratados: vão para uma planilha de revisão para um humano atuar.

## Como funciona (resumo)

1. **Entrada** — `glpi.csv` exportado da lista de chamados do GLPI (colunas **ID, Categoria, Status, Descrição**).
   Os chamados novos do XCash entram na `chamados.xlsx`. Nome e matrícula saem do formulário da Descrição.
2. **CASI** — pesquisa a matrícula, confere o nome, clica no lápis e grava `Dpsp@` + matrícula (sem o zero à
   esquerda). Se o CASI disser "Senha já utilizada", tenta `Dpsp!`, `Dpsp#`, ... Monta a mensagem de acesso.
3. **GLPI** (opcional) — abre o chamado pela URL, escreve a solução, marca **Solucionado** e clica em Adicionar.
4. **Revisão humana** — pedir troca de loja, criar usuário, nome que não bate, matrícula inexistente ou erro
   técnico geram `revisao_humana.xlsx` para análise.

Categorias atendidas: **Reset de senha** (2314030) e **Criação de usuário XCash**. Na Criação, se o usuário já
existe no CASI a automação reseta (igual ao reset); se não existe, vai para revisão (não cria usuário).

## Instalação

```
pip install -r requirements.txt
python -m playwright install chromium   # só se for usar NAVEGADOR=chromium (ex.: no GitLab)
```

Copie `.env.example` para `.env` e preencha `CASI_USUARIO`/`CASI_SENHA`. O `.env` guarda senha: não compartilhe.

## Uso

| Comando | O que faz |
|---|---|
| `python main.py --verificar` | Testa só o login no CASI |
| `python main.py --teste-casi 126594 "Nome"` | Simula uma matrícula, sem planilha, sem salvar |
| `python main.py --simular` | Importa o CSV e passa pelo CASI sem salvar nada |
| `python main.py` | Execução real |
| `python main.py --login-glpi` | Abre o navegador para você logar no GLPI e salva a sessão |
| `python main.py --visivel` | Mostra o navegador |
| `python main.py --reprocessar` | Processa de novo linhas já finalizadas |

Fluxo normal: exporte o `glpi.csv`, feche a `chamados.xlsx` no Excel e rode `python main.py`.
Resultados na coluna **Resultado automação**: SUCESSO, PENDENTE GLPI (senha trocada, falta responder),
REVISÃO MANUAL, ERRO, IGNORADO. A mensagem de cada chamado fica na coluna **Mensagem GLPI** e em `mensagens/`.

## Resposta automática no GLPI

Desligada por padrão (`RESPONDER_GLPI=false`): a automação só reseta e deixa a mensagem pronta.

Para ligar:
1. `RESPONDER_GLPI=true` no `.env`.
2. `python main.py --login-glpi` e faça o login (pode usar o SSO). A sessão fica em `glpi_session.json`.
3. Primeiro rode com `GLPI_SUBMETER=false`: a automação **preenche** a solução mas **não** clica em Adicionar.
   Confira os prints em `evidencias/`. Se estiver certo, mude para `GLPI_SUBMETER=true`.

Observações:
- Se resetar no CASI mas falhar ao responder, a linha fica **PENDENTE GLPI**: na próxima execução ela só
  reenvia a resposta (reaproveita a Mensagem GLPI) e **não** reseta de novo.
- A sessão do GLPI expira. Quando isso acontecer, a automação avisa para rodar `--login-glpi` de novo.

## GitLab (a cada 2h)

Veja `.gitlab-ci.yml`. Pontos importantes:
- **Rede**: CASI (`192.168.57.78`) e GLPI são internos. Runners do gitlab.com não os alcançam — é preciso um
  **GitLab Runner self-hosted** dentro da rede (ajuste a tag `dpsp-interno`).
- **Agendamento**: em Build > Pipeline schedules, cron `0 */2 * * *`.
- **Segredos**: `CASI_USUARIO` e `CASI_SENHA` em Settings > CI/CD > Variables (Masked/Protected).
- No CI a resposta no GLPI vem **desligada**, porque a sessão por cookie não dura em execução desatendida.
  Para responder sem ninguém por perto, o caminho robusto é a **API do GLPI** (dá para adicionar depois).

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `main.py` | Orquestra o fluxo |
| `casi.py` | Robô do CASI (pesquisa, lápis, senha) |
| `glpi_csv.py` | Lê o CSV exportado do GLPI |
| `glpi_web.py` | Responde o chamado no GLPI (web) |
| `chamado.py` | Regras: lê o formulário, confere o nome, monta a senha |
| `planilha.py` | Lê/grava a `chamados.xlsx` e a `revisao_humana.xlsx` |
| `config.py` | Lê o `.env` |
| `mensagem_glpi.txt` | Modelo da mensagem (use `{usuario}`, `{caractere}`, `{senha}`, ...) |
```
