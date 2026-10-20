"""
Automação do reset de senha do XCash (CASI) a partir dos chamados do GLPI.

Fluxo (ver fluxograma):
  1. Entrada: CSV exportado da lista de chamados do GLPI (colunas ID, Categoria, Status, Descrição).
     Os chamados XCash ainda não importados entram na planilha chamados.xlsx. Nome e matrícula saem
     do formulário que está na Descrição.
  2. CASI: pesquisa a matrícula, confere o nome, clica no lápis e grava Dpsp@ + matrícula (sem zero
     à esquerda). Se o CASI disser "Senha já utilizada", tenta Dpsp!, Dpsp#, ... Monta a mensagem de acesso.
  3. GLPI (opcional, RESPONDER_GLPI): abre o chamado pela URL, escreve a solução com a mensagem,
     marca como Solucionado e clica em Adicionar.
  4. Chamados incertos (pedem trocar loja, criar usuário, nome não bate, matrícula não existe, erro técnico)
     NÃO são tratados: vão para a planilha de revisão (revisao_humana.xlsx) para um humano atuar.

Categorias atendidas:
  ... > Acesso ao Sistema XCash (Tesouraria loja) > Reset de senha (2314030)
  ... > Acesso ao Sistema XCash (Tesouraria loja) > Criação de usuário XCash
      usuário já existe no CASI: reseta como no Reset de senha (CRIACAO_SE_JA_EXISTE=resetar)
      usuário não existe: REVISÃO MANUAL (a automação não cria usuários)

Resultados na planilha: SUCESSO, PENDENTE GLPI (senha trocada, falta responder), REVISÃO MANUAL, ERRO, IGNORADO.

Campos usáveis em mensagem_glpi.txt: {usuario} (126594@dpsp), {matricula}, {caractere}, {senha}, {nome}

Uso:
  python main.py --login-glpi                faz o login no GLPI uma vez e salva a sessão (abre o navegador)
  python main.py --verificar                 testa o login no CASI e sai
  python main.py --teste-casi 126594 "Nome"  simula uma matrícula, sem planilha e sem salvar
  python main.py --simular                   percorre a planilha sem salvar no CASI nem enviar ao GLPI
  python main.py                             execução real
  python main.py --visivel                   mostra o navegador
  python main.py --reprocessar               processa de novo as linhas já finalizadas
  python main.py --planilha x.xlsx           usa outra planilha
  python main.py --csv export.csv            usa outro CSV do GLPI
  python main.py --sem-csv                   não importa o CSV nesta execução
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from collections import Counter
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

import config
from casi import CasiBot, CasiErro, GravacaoRecusada, SenhaJaUtilizada, UsuarioCasi
from chamado import (
    DadosChamado,
    RevisaoManual,
    UsuarioNaoEncontrado,
    ler_caracteres,
    matricula_sem_zeros,
    mesma_matricula,
    montar_dados,
    nomes_conferem,
    senha_padrao,
)
from glpi_csv import CsvErro, ler_csv, mascarar_cpf
from glpi_web import GlpiWeb, GlpiWebErro, PrecisaLoginGlpi, login_interativo
from planilha import (
    ERRO,
    FINALIZADOS,
    IGNORADO,
    PENDENTE_GLPI,
    REVISAO_MANUAL,
    SUCESSO,
    LinhaChamado,
    Planilha,
    PlanilhaErro,
    criar_modelo,
    normalizar_texto,
)

log = logging.getLogger("xcash")

PASTA_MENSAGENS = config.BASE_DIR / "mensagens"
RESET, CRIACAO = "reset", "criacao"


class ErroFatal(Exception):
    """Problema que impede continuar (login, planilha travada...). Interrompe a execução."""


# ===================== infraestrutura =====================
def configurar_log() -> None:
    pasta = config.BASE_DIR / "logs"
    pasta.mkdir(exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(pasta / f"automacao_{datetime.now():%Y-%m-%d}.log", encoding="utf-8"),
        ],
    )


def iniciar_navegador(playwright, cfg: config.Config, visivel: bool) -> Browser:
    opcoes = {"headless": cfg.headless and not visivel}
    if cfg.navegador not in ("", "chromium"):
        opcoes["channel"] = cfg.navegador
    return playwright.chromium.launch(**opcoes)


def abrir_contexto_glpi(playwright, cfg: config.Config, visivel: bool) -> BrowserContext:
    """Abre o GLPI num perfil persistente: o login (inclusive SSO) fica guardado para as próximas vezes."""
    cfg.glpi_perfil.mkdir(parents=True, exist_ok=True)
    opcoes = {"headless": cfg.headless and not visivel, "locale": "pt-BR",
              "viewport": {"width": 1366, "height": 900}}
    if cfg.glpi_navegador not in ("", "chromium"):
        opcoes["channel"] = cfg.glpi_navegador
    return playwright.chromium.launch_persistent_context(str(cfg.glpi_perfil), **opcoes)


def nova_pagina(navegador: Browser, cfg: config.Config) -> Page:
    contexto = navegador.new_context(viewport={"width": 1366, "height": 900}, locale="pt-BR")
    pagina = contexto.new_page()
    pagina.set_default_timeout(cfg.timeout_ms)
    return pagina


# ===================== mensagem =====================
def nome_arquivo(linha: LinhaChamado) -> str:
    base = f"chamado_{linha.chamado}" if linha.chamado else f"linha_{linha.numero}"
    return re.sub(r"[^\w-]", "_", base)


def montar_mensagem(modelo: str, usuario: UsuarioCasi, caractere: str) -> str:
    matricula = usuario.matricula.strip()
    campos = {
        "{usuario}": f"{matricula}@dpsp",
        "{matricula}": matricula,
        "{caractere}": caractere,
        "{senha}": senha_padrao(matricula, caractere),
        "{nome}": usuario.nome,
    }
    for campo, valor in campos.items():
        modelo = modelo.replace(campo, valor)
    return modelo.strip() + "\n"


def salvar_mensagem(linha: LinhaChamado, mensagem: str) -> Path:
    PASTA_MENSAGENS.mkdir(exist_ok=True)
    caminho = PASTA_MENSAGENS / f"{nome_arquivo(linha)}.txt"
    caminho.write_text(mensagem, encoding="utf-8")
    return caminho


# ===================== categorias / CSV =====================
def tipo_do_chamado(valor: str, cfg: config.Config) -> str | None:
    """RESET, CRIACAO ou None. Aceita o caminho completo (com ou sem '(ID)') ou só o ID."""
    texto = normalizar_texto(valor)
    if not texto:
        return None

    def bate(nome: str, id_categoria: str) -> bool:
        if id_categoria and re.search(rf"(?<!\d){re.escape(id_categoria)}(?!\d)", texto):
            return True
        return re.sub(r"\s*\(\d+\)\s*$", "", texto) == normalizar_texto(nome)

    if bate(cfg.categoria_nome, cfg.categoria_id):
        return RESET
    if bate(cfg.categoria_criacao, cfg.categoria_criacao_id):
        return CRIACAO
    return None


def importar_csv(planilha: Planilha, caminho: Path, cfg: config.Config) -> int:
    """Copia para a planilha os chamados XCash do CSV que ainda não estão nela. Reset vem primeiro;
    dentro de cada tipo, do chamado mais antigo para o mais novo."""
    existentes = planilha.chamados_existentes()
    ignorados: Counter[str] = Counter()
    novos = []
    for c in ler_csv(caminho):
        tipo = tipo_do_chamado(c.categoria, cfg)
        if tipo is None:
            ignorados["outra categoria"] += 1
        elif c.encerrado:
            ignorados["solucionado/fechado"] += 1
        elif not c.chamado:
            ignorados["sem número"] += 1
        elif c.chamado in existentes:
            ignorados["já estavam na planilha"] += 1
        else:
            novos.append((0 if tipo == RESET else 1, int(c.chamado), c))
            existentes.add(c.chamado)

    for _, _, c in sorted(novos, key=lambda n: (n[0], n[1])):
        planilha.adicionar(c.chamado, c.categoria, c.nome, c.matricula, mascarar_cpf(c.descricao))
    if novos:
        planilha.salvar()
    por_tipo = Counter("Reset" if n[0] == 0 else "Criação" for n in novos)
    log.info("CSV %s: %d chamado(s) novo(s) na planilha (%s). Fora: %s.", caminho.name, len(novos),
             ", ".join(f"{t}={q}" for t, q in por_tipo.items()) or "nenhum",
             ", ".join(f"{m}={q}" for m, q in ignorados.items()) or "nenhum")
    return len(novos)


# ===================== CASI =====================
def localizar_usuario(casi: CasiBot, dados: DadosChamado) -> UsuarioCasi:
    """Pesquisa pela matrícula e confere o nome. Levanta RevisaoManual/UsuarioNaoEncontrado se não bater."""
    matricula = matricula_sem_zeros(dados.matricula)
    usuarios = casi.pesquisar("Matrícula", matricula)
    encontrados = [u for u in usuarios if mesma_matricula(u.matricula, matricula)]
    if not encontrados and dados.matricula != matricula:  # tenta como veio (com zero à esquerda)
        encontrados = [u for u in casi.pesquisar("Matrícula", dados.matricula)
                       if mesma_matricula(u.matricula, matricula)]

    if not encontrados:
        dica = ""
        try:  # pesquisa pelo nome só para ajudar o analista; não reseta nesse caso
            parecidos = [u for u in casi.pesquisar("Nome", dados.nome) if nomes_conferem(dados.nome, u.nome)[0]]
            if parecidos:
                dica = " Pelo nome, o CASI tem: " + "; ".join(f"{u.matricula} - {u.nome}" for u in parecidos[:5]) + "."
        except CasiErro as exc:
            log.warning("Pesquisa por nome falhou: %s", exc)
        raise UsuarioNaoEncontrado(f"Matrícula {matricula} não encontrada no CASI.{dica}")

    if len(encontrados) > 1:
        lista = "; ".join(f"{u.matricula} - {u.nome}" for u in encontrados)
        raise RevisaoManual(f"Mais de um usuário no CASI com a matrícula {matricula}: {lista}.")

    usuario = encontrados[0]
    confere, detalhe = nomes_conferem(dados.nome, usuario.nome)
    if not confere:
        raise RevisaoManual(
            f"O nome informado ('{dados.nome}') não bate com o do CASI para a matrícula "
            f"{usuario.matricula} ('{usuario.nome}'): {detalhe}."
        )
    log.info("Conferência OK: matrícula %s = %s (%s).", usuario.matricula, usuario.nome, detalhe)
    if usuario.bloqueado:
        log.warning("Atenção: o usuário %s está marcado como bloqueado no CASI.", usuario.matricula)
    return usuario


def executar_reset(casi: CasiBot, dados: DadosChamado, caracteres: list[str], simular: bool,
                   nome_evidencia: str) -> tuple[UsuarioCasi, str, list[str]]:
    """Tenta Dpsp + caractere + matrícula, passando ao próximo caractere só quando o CASI responde
    'Senha já utilizada'. Devolve (usuário, caractere aceito, recusas). Levanta RevisaoManual se o CASI
    recusar todos ou recusar por outro motivo."""
    recusas: list[str] = []
    for tentativa, caractere in enumerate(caracteres, start=1):
        usuario = localizar_usuario(casi, dados)
        senha = senha_padrao(usuario.matricula, caractere)  # matrícula do CASI, sem zero à esquerda
        try:
            casi.resetar_senha(usuario, senha, simular, f"{nome_evidencia}_t{tentativa}")
            return usuario, caractere, recusas
        except SenhaJaUtilizada as exc:
            recusas.append(f"Dpsp{caractere} já utilizada")
            log.warning("CASI: Dpsp%s já utilizada por %s (%s). Tentando o próximo caractere.",
                        caractere, usuario.matricula, exc)
        except GravacaoRecusada as exc:
            antes = f" Antes: {' | '.join(recusas)}." if recusas else ""
            raise RevisaoManual(f"O CASI não aceitou salvar Dpsp{caractere}: {exc}.{antes}") from exc
    raise RevisaoManual("Todas as variações de senha já foram utilizadas por este usuário: "
                        + " | ".join(recusas) + ". Defina outro caractere na coluna 'Caractere senha'.")


def caracteres_da_linha(linha: LinhaChamado, cfg: config.Config) -> list[str]:
    if not linha.caractere:
        return list(cfg.caracteres_senha)
    caracteres = ler_caracteres(linha.caractere)
    if not caracteres:
        raise RevisaoManual(f"'Caractere senha' inválido na planilha: '{linha.caractere}' (use um símbolo, ex.: # ou !).")
    return caracteres


# ===================== GLPI =====================
def enviar_ao_glpi(obter_glpi, numero: str, mensagem: str, cfg: config.Config, submeter: bool) -> tuple[bool, str]:
    try:
        detalhe = obter_glpi().responder(numero, mensagem, cfg.glpi_status_solucao, submeter)
        return True, detalhe
    except PrecisaLoginGlpi as exc:
        raise ErroFatal(str(exc)) from exc
    except GlpiWebErro as exc:
        return False, f"Falha ao responder no GLPI: {exc}"


def processar_linha(linha: LinhaChamado, obter_casi, obter_glpi, cfg: config.Config, simular: bool,
                    submeter_glpi: bool, modelo: str, resetadas: dict,
                    forcar_glpi: bool = False) -> tuple[str, str, str]:
    """Devolve (resultado, detalhe, mensagem) para gravar na planilha."""
    tipo = tipo_do_chamado(linha.categoria, cfg)
    if tipo is None:
        if cfg.exigir_categoria:
            return IGNORADO, (f"Categoria '{linha.categoria or 'vazia'}' não é Reset de senha nem Criação de usuário "
                              "do XCash. Nada foi feito."), ""
        tipo = RESET

    # Só responder o GLPI (reset já feito antes, ou pedido explícito com --so-glpi): não mexe no CASI
    if forcar_glpi or linha.resultado == PENDENTE_GLPI:
        if not linha.mensagem.strip():
            raise RevisaoManual("Esta linha não tem a Mensagem GLPI salva. Rode o reset no CASI primeiro "
                                "(sem --so-glpi) para gerar a mensagem.")
        if not linha.chamado:
            return SUCESSO, "Sem número do chamado; responda manualmente com a Mensagem GLPI.", linha.mensagem
        if not cfg.responder_glpi:
            return SUCESSO, "RESPONDER_GLPI desligado; responda o chamado manualmente.", linha.mensagem
        ok, detalhe = enviar_ao_glpi(obter_glpi, linha.chamado, linha.mensagem, cfg, submeter_glpi)
        enviado = ok and submeter_glpi
        return (SUCESSO if enviado else PENDENTE_GLPI), f"(reset já feito) {detalhe}", linha.mensagem

    dados = montar_dados(linha.nome, linha.matricula, linha.descricao)
    caracteres = caracteres_da_linha(linha, cfg)
    matricula = matricula_sem_zeros(dados.matricula)

    if matricula in resetadas:
        referencia, caractere, mensagem = resetadas[matricula]
        detalhe = f"Mesma matrícula já resetada nesta execução ({referencia}) com Dpsp{caractere}."
    else:
        log.info("%s (%s): colaborador '%s', matrícula %s.", linha.referencia,
                 "criação" if tipo == CRIACAO else "reset", dados.nome, dados.matricula)
        casi = obter_casi()
        prefixo = ""
        if tipo == CRIACAO:
            try:
                existente = localizar_usuario(casi, dados)
            except UsuarioNaoEncontrado as exc:
                raise RevisaoManual("Chamado de criação: o usuário ainda não existe no CASI e precisa ser criado "
                                    f"(a automação ainda não cria usuários). {exc}") from exc
            if cfg.criacao_se_ja_existe != "resetar":
                raise RevisaoManual(
                    f"Chamado de criação, mas o usuário já existe no CASI ({existente.matricula} - {existente.nome}). "
                    "Para a automação resetar nesses casos, use CRIACAO_SE_JA_EXISTE=resetar no .env."
                )
            prefixo = "Chamado de criação, mas o usuário já existia no CASI. "
        usuario, caractere, recusas = executar_reset(casi, dados, caracteres, simular, nome_arquivo(linha))
        mensagem = montar_mensagem(modelo, usuario, caractere)
        resetadas[matricula] = (linha.referencia, caractere, mensagem)

        acao = "Senha seria resetada" if simular else "Senha resetada"
        detalhe = f"{prefixo}{acao} no CASI para {usuario.matricula} - {usuario.nome} com Dpsp{caractere}."
        if recusas:
            detalhe += " Antes: " + " | ".join(recusas) + "."

    if not simular:
        salvar_mensagem(linha, mensagem)

    # Resposta no GLPI
    if not (cfg.responder_glpi and linha.chamado):
        if cfg.responder_glpi:
            detalhe += " Sem número do chamado: responda manualmente com a Mensagem GLPI."
        else:
            detalhe += " Responda o chamado com a Mensagem GLPI."
        return SUCESSO, detalhe, mensagem

    ok, detalhe_glpi = enviar_ao_glpi(obter_glpi, linha.chamado, mensagem, cfg, submeter_glpi)
    detalhe += " " + detalhe_glpi
    if ok and submeter_glpi:
        return SUCESSO, detalhe, mensagem
    if not ok:
        detalhe += " A senha JÁ foi trocada; a resposta será reenviada na próxima execução."
    return PENDENTE_GLPI, detalhe, mensagem


# ===================== principal =====================
def ler_argumentos() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reset de senha XCash (CASI) e resposta no GLPI.")
    p.add_argument("--login-glpi", action="store_true", help="abre o navegador para você logar no GLPI e salva a sessão")
    p.add_argument("--verificar", action="store_true", help="testa o login no CASI e sai")
    p.add_argument("--simular", action="store_true", help="não salva no CASI e não envia ao GLPI")
    p.add_argument("--visivel", action="store_true", help="mostra o navegador")
    p.add_argument("--reprocessar", action="store_true", help="processa de novo linhas já finalizadas")
    p.add_argument("--planilha", type=Path, help="caminho da planilha (padrão: PLANILHA do .env)")
    p.add_argument("--csv", type=Path, help="CSV exportado do GLPI (padrão: CSV_GLPI do .env)")
    p.add_argument("--sem-csv", action="store_true", help="não importa o CSV nesta execução")
    p.add_argument("--chamado", help="processa SÓ este número de chamado (ignora os demais)")
    p.add_argument("--so-glpi", action="store_true",
                   help="só responde no GLPI usando a Mensagem GLPI já salva (não mexe no CASI)")
    p.add_argument("--teste-casi", nargs=2, metavar=("MATRICULA", "NOME"),
                   help="simula só o CASI para uma matrícula/nome, sem planilha (nunca salva)")
    return p.parse_args()


def main() -> int:
    args = ler_argumentos()
    cfg = config.carregar()
    configurar_log()
    simular = args.simular or args.teste_casi is not None
    submeter_glpi = (not simular) and cfg.glpi_submeter
    modelo = (config.BASE_DIR / "mensagem_glpi.txt").read_text(encoding="utf-8")

    with ExitStack() as pilha:
        estado: dict[str, object] = {}

        def playwright():
            if "pw" not in estado:
                estado["pw"] = pilha.enter_context(sync_playwright())
            return estado["pw"]

        def navegador() -> Browser:  # navegador do CASI
            if "navegador" not in estado:
                nav = iniciar_navegador(playwright(), cfg, args.visivel)
                pilha.callback(nav.close)
                estado["navegador"] = nav
            return estado["navegador"]  # type: ignore[return-value]

        def contexto_glpi(visivel: bool) -> BrowserContext:
            ctx = abrir_contexto_glpi(playwright(), cfg, visivel)
            pilha.callback(ctx.close)
            return ctx

        # ---------- login no GLPI ----------
        if args.login_glpi:
            try:
                login_interativo(contexto_glpi(visivel=True), cfg.glpi_url, cfg.timeout_ms)
                return 0
            except GlpiWebErro as exc:
                log.error("%s", exc)
                return 1

        if simular:
            log.info("MODO SIMULAÇÃO: nada será salvo no CASI nem enviado ao GLPI.")
        if cfg.responder_glpi and not simular and not cfg.glpi_submeter:
            log.info("RESPONDER_GLPI ligado, mas GLPI_SUBMETER=false: a solução será preenchida e NÃO enviada.")

        def obter_casi() -> CasiBot:
            if "casi" not in estado:
                try:
                    bot = CasiBot(nova_pagina(navegador(), cfg), cfg.casi_url, cfg.casi_usuario, cfg.casi_senha,
                                  cfg.timeout_ms, config.BASE_DIR / "evidencias")
                    bot.abrir_consulta()
                except Exception as exc:
                    raise ErroFatal(f"Não consegui entrar no CASI: {exc}") from exc
                log.info("Login no CASI OK.")
                estado["casi"] = bot
            return estado["casi"]  # type: ignore[return-value]

        def obter_glpi() -> GlpiWeb:
            if "glpi" not in estado:
                estado["glpi"] = GlpiWeb(contexto_glpi(args.visivel), cfg.glpi_url, cfg.timeout_ms,
                                         config.BASE_DIR / "evidencias")
            return estado["glpi"]  # type: ignore[return-value]

        def print_casi(nome: str) -> None:
            if "casi" in estado:
                caminho = estado["casi"].evidencia(nome)  # type: ignore[union-attr]
                if caminho:
                    log.info("Print do CASI salvo em: %s", caminho)

        # ---------- testes rápidos ----------
        if args.verificar:
            try:
                obter_casi()
            except ErroFatal as exc:
                log.error("%s", exc)
                return 1
            log.info("Verificação concluída: login no CASI OK.")
            return 0

        if args.teste_casi:
            matricula, nome = args.teste_casi
            try:
                dados = DadosChamado(nome=nome, matricula=re.sub(r"\D", "", matricula))
                usuario, caractere, _ = executar_reset(obter_casi(), dados, list(cfg.caracteres_senha),
                                                        simular=True, nome_evidencia="teste_casi")
                log.info("Teste concluído: usuário encontrado, nome confere e senha preenchida (NÃO salva).")
                log.info("Mensagem que iria para o GLPI:\n%s", montar_mensagem(modelo, usuario, caractere))
                return 0
            except RevisaoManual as exc:
                log.warning("Iria para REVISÃO MANUAL: %s", exc)
                return 0
            except Exception:
                log.exception("Falha no teste do CASI.")
                print_casi("teste_casi_erro")
                return 1

        # ---------- planilha ----------
        caminho = args.planilha.resolve() if args.planilha else cfg.planilha
        if not caminho.exists():
            criar_modelo(caminho)
            log.info("Planilha modelo criada em %s.", caminho)
        try:
            planilha = Planilha(caminho)
        except PlanilhaErro as exc:
            log.error("%s", exc)
            return 1
        if cfg.exigir_categoria and not planilha.tem_coluna_categoria:
            log.error("A planilha não tem a coluna 'Categoria' (obrigatória para filtrar só os chamados do XCash).")
            return 1

        # ---------- CSV do GLPI ----------
        caminho_csv = args.csv.resolve() if args.csv else cfg.csv_glpi
        if not args.sem_csv and caminho_csv is not None:
            if not caminho_csv.exists():
                if args.csv:
                    log.error("CSV não encontrado: %s", caminho_csv)
                    return 1
                log.info("Sem %s na pasta; usando só o que já está na planilha.", caminho_csv.name)
            elif not planilha.tem_coluna_chamado:
                log.error("A planilha não tem a coluna 'Chamado', necessária para importar o CSV sem duplicar.")
                return 1
            else:
                try:
                    importar_csv(planilha, caminho_csv, cfg)
                except (CsvErro, PlanilhaErro) as exc:
                    log.error("%s", exc)
                    return 1

        linhas = [l for l in planilha.linhas() if not l.vazia]
        alvo = re.sub(r"\D", "", args.chamado) if args.chamado else None
        if alvo:
            linhas = [l for l in linhas if re.sub(r"\D", "", l.chamado) == alvo]
            if not linhas:
                log.error("Chamado %s não está na planilha %s.", args.chamado, caminho.name)
                return 1
            log.info("Processando SÓ o chamado %s.", alvo)
        log.info("Planilha %s: %d chamado(s) para analisar.", caminho.name, len(linhas))
        resumo: Counter[str] = Counter()
        resetadas: dict = {}
        primeiro = True

        for linha in linhas:
            # --chamado é um pedido explícito: processa mesmo que já esteja finalizado
            if not args.reprocessar and not alvo and linha.resultado in FINALIZADOS:
                resumo["pulado"] += 1
                continue
            if not primeiro and cfg.pausa_entre_chamados_s:
                time.sleep(cfg.pausa_entre_chamados_s)  # gentileza com GLPI/CASI
            primeiro = False

            mensagem = ""
            try:
                resultado, detalhe, mensagem = processar_linha(linha, obter_casi, obter_glpi, cfg, simular,
                                                               submeter_glpi, modelo, resetadas,
                                                               forcar_glpi=args.so_glpi)
            except ErroFatal as exc:
                log.error("%s Execução interrompida; as linhas restantes ficaram para a próxima.", exc)
                print_casi("erro_fatal")
                resumo["interrompido"] += 1
                break
            except RevisaoManual as exc:
                resultado, detalhe = REVISAO_MANUAL, str(exc)
            except Exception as exc:  # erro técnico: tentado de novo na próxima execução
                log.exception("%s: erro técnico.", linha.referencia)
                print_casi(f"linha_{linha.numero}_erro")
                resultado, detalhe = ERRO, f"Erro técnico (será tentado de novo): {exc}"

            log.log(logging.INFO if resultado == SUCESSO else logging.WARNING,
                    "%s: %s - %s", linha.referencia, resultado, detalhe)
            resumo[resultado] += 1
            try:
                planilha.registrar(linha, resultado, detalhe, mensagem, simular)
            except PlanilhaErro as exc:
                log.error("%s O resultado da %s ficou só no log. Execução interrompida.", exc, linha.referencia)
                break

        pendentes = planilha.exportar_revisao(config.BASE_DIR / "revisao_humana.xlsx")
        if pendentes:
            log.info("%d chamado(s) para um humano revisar em revisao_humana.xlsx.", pendentes)

    log.info("Resumo: %s", ", ".join(f"{k}={v}" for k, v in sorted(resumo.items())) or "nada a processar")
    return 1 if resumo[ERRO] or resumo["interrompido"] else 0


if __name__ == "__main__":
    sys.exit(main())
