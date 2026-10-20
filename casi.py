"""Automação da tela Controle Acesso > Usuário do CASI (JSF/PrimeFaces 6.1) com Playwright."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from chamado import mesma_matricula, normalizar_texto

log = logging.getLogger("xcash.casi")

# Página "parada": carregada e sem AJAX do PrimeFaces/jQuery em andamento
_JS_AJAX_OCIOSO = """() => {
    if (document.readyState !== 'complete') return false;
    const pf = window.PrimeFaces && window.PrimeFaces.ajax && window.PrimeFaces.ajax.Queue;
    if (pf && !window.PrimeFaces.ajax.Queue.isEmpty()) return false;
    if (window.jQuery && window.jQuery.active > 0) return false;
    return true;
}"""

# Texto da célula sem o título de coluna que o PrimeFaces injeta no modo responsivo
_JS_TEXTO_CELULA = """el => {
    const c = el.cloneNode(true);
    c.querySelectorAll('.ui-column-title').forEach(x => x.remove());
    return (c.textContent || '').replace(/\\s+/g, ' ').trim();
}"""


def _rotulo(regex: str) -> re.Pattern:
    """Regex para o texto inteiro de um rótulo, tolerando '*' (obrigatório) e ':' no final."""
    return re.compile(rf"^\s*{regex}\s*\*?\s*:?\s*$", re.IGNORECASE)


class CasiErro(Exception):
    pass


class SenhaJaUtilizada(CasiErro):
    """CASI: 'Senha já utilizada anteriormente. Digite outra senha' -> tentar o próximo caractere."""


class GravacaoRecusada(CasiErro):
    """O CASI recusou o Salvar por outro motivo; trocar o caractere da senha não resolve."""


# Texto que o CASI mostra quando a senha nova já foi usada antes
_SENHA_JA_UTILIZADA = re.compile(r"senha\s+j[aá]\s+(foi\s+)?utilizada", re.IGNORECASE)
_SALVO_COM_SUCESSO = re.compile(r"salvo\s+com\s+sucesso", re.IGNORECASE)


# Apaga mensagens antigas da tela para que só a resposta do próximo Salvar seja considerada
_JS_LIMPAR_MENSAGENS = """() => {
    document.querySelectorAll('.ui-messages, .ui-growl').forEach(e => { e.innerHTML = ''; });
    document.querySelectorAll('.ui-message').forEach(e => {
        e.innerHTML = '';
        e.className = e.className.replace(/\\bui-message-(info|warn|error|fatal)\\b/g, '');
    });
}"""


@dataclass(frozen=True)
class UsuarioCasi:
    linha: int  # posição da linha na tabela de resultados
    matricula: str
    nome: str
    bloqueado: bool | None


class CasiBot:
    def __init__(self, page: Page, base_url: str, usuario: str, senha: str, timeout_ms: int, pasta_evidencias: Path):
        self.page = page
        self.usuario = usuario
        self.senha = senha
        self.timeout_ms = timeout_ms
        self.pasta_evidencias = pasta_evidencias
        self.url_consulta = f"{base_url.rstrip('/')}/cadastros/UsuarioConsulta.xhtml"
        # Caso o CASI mostre a resposta numa janela de alerta do navegador, guarda o texto e fecha
        self._alertas: list[str] = []
        page.on("dialog", self._ao_abrir_alerta)

    def _ao_abrir_alerta(self, dialogo) -> None:
        self._alertas.append(" ".join((dialogo.message or "").split()))
        log.info("Alerta do CASI: %s", self._alertas[-1])
        dialogo.accept()

    # ================= utilidades =================
    def _aguardar_ajax(self) -> None:
        self.page.wait_for_timeout(250)  # dá tempo da requisição entrar na fila
        limite = time.monotonic() + self.timeout_ms / 1000
        while True:
            try:
                self.page.wait_for_function(_JS_AJAX_OCIOSO, timeout=self.timeout_ms)
                return
            except PlaywrightTimeout as exc:
                raise CasiErro("O CASI não respondeu a tempo (requisição AJAX não terminou).") from exc
            except PlaywrightError:
                # contexto destruído por navegação de página: tenta de novo
                if time.monotonic() > limite:
                    raise
                self.page.wait_for_timeout(300)

    def evidencia(self, nome: str) -> Path | None:
        """Print da tela para auditoria/diagnóstico. Campos de senha aparecem mascarados."""
        try:
            pasta = self.pasta_evidencias / datetime.now().strftime("%Y-%m-%d")
            pasta.mkdir(parents=True, exist_ok=True)
            caminho = pasta / f"{datetime.now():%H%M%S}_{nome}.png"
            self.page.screenshot(path=str(caminho), full_page=True)
            return caminho
        except PlaywrightError as exc:
            log.warning("Não consegui salvar print da tela: %s", exc)
            return None

    def _label_visivel(self, padrao: re.Pattern) -> Locator | None:
        labels = self.page.locator("label").filter(has_text=padrao)
        for i in range(labels.count()):
            if labels.nth(i).is_visible():
                return labels.nth(i)
        return None

    def _campo_por_label(self, padrao: re.Pattern) -> Locator | None:
        label = self._label_visivel(padrao)
        alvo = label.get_attribute("for") if label else None
        if not alvo:
            return None
        campo = self.page.locator(f"[id='{alvo}']")
        return campo.first if campo.count() else None

    @staticmethod
    def _valor_campo(campo: Locator) -> str:
        tag = campo.evaluate("e => e.tagName.toLowerCase()")
        if tag in ("input", "textarea", "select"):
            return campo.input_value()
        interno = campo.locator("input")
        return interno.first.input_value() if interno.count() else campo.inner_text()

    def _botao(self, texto: str) -> Locator:
        """Acha um botão pelo texto EXATO (ex.: 'Salvar'), nunca por outro parecido ('Excluir'/'Voltar').
        No CASI o Salvar é <button><span class="ui-button-text">Salvar</span></button> (PrimeFaces)."""
        padrao = re.compile(rf"^\s*{re.escape(texto)}\s*$", re.IGNORECASE)
        candidatos = self.page.locator(
            "button, a.ui-button, a[role='button'], input[type='submit'], input[type='button']"
        ).filter(visible=True)
        for i in range(candidatos.count()):
            alvo = candidatos.nth(i)
            try:
                rotulo = (alvo.get_attribute("value") or "").strip() or alvo.inner_text(timeout=1000).strip()
            except PlaywrightError:
                continue
            if padrao.match(rotulo):
                return alvo
        por_papel = self.page.get_by_role("button", name=padrao).filter(visible=True)
        if por_papel.count():
            return por_papel.first
        raise CasiErro(f"Botão '{texto}' não encontrado na tela do CASI.")

    # ================= login / navegação =================
    def _tela_login_aberta(self) -> bool:
        try:
            return self.page.locator("[id='frmLogin:password']").count() > 0
        except PlaywrightError:
            return False

    def _login(self) -> None:
        log.info("Fazendo login no CASI com o usuário %s...", self.usuario)
        form = self.page.locator("[id='frmLogin']")
        # O placeholder muda com o idioma ("Usuário"/"Utilizador"); o form só tem um campo de texto visível
        campo_usuario = form.locator("input[type='text']").filter(visible=True)
        if not campo_usuario.count():
            raise CasiErro("Campo de usuário não encontrado na tela de login do CASI.")
        campo_usuario.first.fill(self.usuario)
        self.page.locator("[id='frmLogin:password']").fill(self.senha)
        self.page.locator("[id='frmLogin:pButtonLogar']").click()

        limite = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < limite:
            self.page.wait_for_timeout(400)
            try:
                # Depois do login o CASI redireciona sozinho para home.xhtml; espera esse redirecionamento terminar
                if "home.xhtml" in self.page.url:
                    self.page.wait_for_load_state("domcontentloaded")
                    return
                if not self._tela_login_aberta():
                    self.page.wait_for_timeout(1500)  # pode haver redirecionamento a caminho
                    if not self._tela_login_aberta():
                        return
                dialogo = self.page.locator("[id='form:idDialogSenhaExpirada']")
                if dialogo.count() and dialogo.is_visible():
                    raise CasiErro(
                        "A senha do usuário do CASI usado pela automação expirou. "
                        "Troque a senha manualmente no CASI e atualize CASI_SENHA no .env."
                    )
                mensagem = self.page.locator("[id='frmLogin:idMsgLogin']").inner_text(timeout=1000).strip()
                if mensagem:
                    raise CasiErro(f"Login no CASI recusado: {mensagem}")
            except PlaywrightError:
                continue  # página navegando
        raise CasiErro("Tempo esgotado aguardando o login no CASI.")

    def _ir_para_consulta(self) -> None:
        """Abre a tela de usuários. Se um redirecionamento do CASI atropelar, espera e tenta de novo."""
        for tentativa in range(1, 4):
            try:
                self.page.goto(self.url_consulta, wait_until="domcontentloaded")
                self._aguardar_ajax()
                return
            except PlaywrightError as exc:
                if "interrupted by another navigation" not in str(exc) or tentativa == 3:
                    raise
                log.info("O CASI redirecionou no meio do caminho; abrindo a tela de usuários de novo.")
                self.page.wait_for_load_state("domcontentloaded")
                self.page.wait_for_timeout(1000)

    def abrir_consulta(self) -> None:
        """Abre Controle Acesso > Usuário, fazendo login se necessário."""
        self._ir_para_consulta()
        if self._tela_login_aberta():
            self._login()
            self._ir_para_consulta()
            if self._tela_login_aberta():
                raise CasiErro("Não foi possível autenticar no CASI.")
        self._botao("Pesquisar").wait_for(state="visible", timeout=self.timeout_ms)

    # ================= pesquisa =================
    def _selecionar_tipo_pesquisa(self, tipo: str) -> None:
        alvo = normalizar_texto(tipo)
        menu = None

        label = self._label_visivel(_rotulo(r"Pesquisar\s+por"))
        alvo_label = label.get_attribute("for") if label else None
        if alvo_label:
            candidato = self.page.locator(f"[id='{re.sub(r'_(focus|input)$', '', alvo_label)}']")
            if candidato.count() and "ui-selectonemenu" in (candidato.first.get_attribute("class") or ""):
                menu = candidato.first
        if menu is None:
            candidatos = self.page.locator("div.ui-selectonemenu").filter(visible=True)
            if candidatos.count():
                menu = candidatos.first

        if menu is None:  # <select> HTML comum
            select = self.page.locator("select:not(.ui-paginator-rpp-options)").filter(visible=True)
            if not select.count():
                raise CasiErro("Campo 'Pesquisar por' não encontrado.")
            for opcao in select.first.locator("option").all():
                if normalizar_texto(opcao.inner_text()) == alvo:
                    select.first.select_option(value=opcao.get_attribute("value"))
                    self._aguardar_ajax()
                    return
            raise CasiErro(f"Opção '{tipo}' não existe em 'Pesquisar por'.")

        rotulo = menu.locator(".ui-selectonemenu-label")
        if normalizar_texto(rotulo.inner_text()) == alvo:
            return
        menu.locator(".ui-selectonemenu-trigger").click()
        painel = self.page.locator(f"[id='{menu.get_attribute('id')}_panel']")
        painel.wait_for(state="visible", timeout=self.timeout_ms)
        for item in painel.locator("li.ui-selectonemenu-item").all():
            if normalizar_texto(item.get_attribute("data-label") or item.inner_text()) == alvo:
                # Trocar o filtro recria o campo Critério no servidor; só segue depois que ele voltar,
                # senão o valor digitado cai num campo que vai ser substituído (pesquisa sai vazia)
                self._executar_e_aguardar_resposta(item.click, f"selecionar '{tipo}'")
                break
        else:
            raise CasiErro(f"Opção '{tipo}' não existe em 'Pesquisar por'.")
        if normalizar_texto(rotulo.inner_text()) != alvo:
            raise CasiErro(f"Não consegui selecionar '{tipo}' em 'Pesquisar por'.")

    def _campo_criterio(self) -> Locator:
        # O rótulo "Critério" não tem 'for'; o campo fica no painel idPanelCriterio, recriado ao trocar o filtro
        no_painel = self.page.locator(
            "[id$='idPanelCriterio'] input[type='text']:not([readonly]):not([disabled])"
        ).filter(visible=True)
        if no_painel.count():
            return no_painel.first
        campo = self._campo_por_label(_rotulo(r"Crit[ée]rio"))
        if campo is not None and campo.is_visible():
            return campo
        candidatos = self.page.locator("input.ui-inputfield[type='text']:not([readonly]):not([disabled])")
        candidatos = candidatos.filter(visible=True)
        if not candidatos.count():
            raise CasiErro("Campo 'Critério' não encontrado.")
        return candidatos.first

    def _tabela_usuarios(self) -> Locator:
        cabecalho = self.page.locator("thead th").filter(has_text=re.compile(r"Matr[ií]cula", re.IGNORECASE))
        tabela = self.page.locator("div.ui-datatable").filter(has=cabecalho)
        if not tabela.count():
            raise CasiErro("Tabela de usuários não encontrada após a pesquisa.")
        return tabela.first

    def _executar_e_aguardar_resposta(self, acao, descricao: str) -> None:
        """Executa a ação e espera a resposta do servidor (o PrimeFaces pode atrasar o envio da requisição)."""
        try:
            with self.page.expect_response(lambda r: r.request.method == "POST", timeout=self.timeout_ms):
                acao()
        except PlaywrightTimeout as exc:
            raise CasiErro(f"O CASI não respondeu a tempo ({descricao}).") from exc
        self._aguardar_ajax()

    @staticmethod
    def _criterio_confere(tipo: str, digitado: str, valor: str) -> bool:
        if normalizar_texto(tipo).startswith("matricula"):
            # o campo tem máscara: "131452" aparece como "131452___"
            return re.sub(r"\D", "", digitado) == re.sub(r"\D", "", valor)
        return normalizar_texto(digitado) == normalizar_texto(valor)

    def _digitar_criterio(self, tipo: str, valor: str) -> None:
        """Digita no Critério. O CASI pode reabrir a tela com a pesquisa anterior no campo, então limpa
        com Ctrl+A + Delete (fill('') não limpa campo com máscara) e confere o que ficou."""
        digitado = ""
        for tentativa in range(1, 4):
            campo = self._campo_criterio()
            campo.click()
            campo.press("Control+a")
            campo.press("Delete")
            if tentativa < 3:
                campo.press_sequentially(valor, delay=40)  # digitação real, respeita a máscara
            else:
                campo.fill(valor)
            digitado = campo.input_value()
            if self._criterio_confere(tipo, digitado, valor):
                return
            log.info("Critério ficou '%s' em vez de '%s'; digitando de novo.", digitado, valor)
            self.page.wait_for_timeout(500)
        raise CasiErro(f"O campo Critério ficou com '{digitado}' em vez de '{valor}'.")

    def pesquisar(self, tipo: str, valor: str) -> list[UsuarioCasi]:
        """Pesquisa na tela de usuários. tipo = 'Matrícula' ou 'Nome'."""
        self.abrir_consulta()
        self._selecionar_tipo_pesquisa(tipo)
        self._digitar_criterio(tipo, valor)
        enviado = self._campo_criterio().input_value()
        self._executar_e_aguardar_resposta(self._botao("Pesquisar").click, "Pesquisar")
        usuarios = self._ler_resultados()
        if not usuarios:
            log.info("CASI - pesquisa por %s sem resultado (Critério enviado: '%s').", tipo, enviado)
        log.info("CASI - pesquisa por %s '%s': %d resultado(s).", tipo, valor, len(usuarios))
        return usuarios

    def _ler_resultados(self) -> list[UsuarioCasi]:
        tabela = self._tabela_usuarios()
        indices: dict[str, int] = {}
        cabecalhos = tabela.locator("thead th")
        for i in range(cabecalhos.count()):
            titulo = normalizar_texto(cabecalhos.nth(i).inner_text())
            if "matricula" not in indices and titulo.startswith("matricula"):
                indices["matricula"] = i
            elif "nome" not in indices and titulo.startswith("nome") and "social" not in titulo:
                indices["nome"] = i
            elif "bloqueado" not in indices and "bloqueado" in titulo:
                indices["bloqueado"] = i
        if "matricula" not in indices or "nome" not in indices:
            raise CasiErro("Não reconheci as colunas Matrícula/Nome na tabela de usuários.")

        usuarios = []
        linhas = tabela.locator("tbody.ui-datatable-data > tr")
        for i in range(linhas.count()):
            linha = linhas.nth(i)
            if "ui-datatable-empty-message" in (linha.get_attribute("class") or ""):
                continue
            celulas = linha.locator(":scope > td")
            matricula = celulas.nth(indices["matricula"]).evaluate(_JS_TEXTO_CELULA)
            nome = celulas.nth(indices["nome"]).evaluate(_JS_TEXTO_CELULA)
            bloqueado = None
            if "bloqueado" in indices:
                marcado = celulas.nth(indices["bloqueado"]).locator(
                    ".ui-chkbox-box.ui-state-active, input[type='checkbox']:checked, .ui-icon-check, .fa-check"
                )
                bloqueado = marcado.count() > 0
            usuarios.append(UsuarioCasi(linha=i, matricula=matricula, nome=nome, bloqueado=bloqueado))
        return usuarios

    # ================= edição / reset =================
    def _campos_senha(self) -> tuple[Locator, Locator]:
        senhas_visiveis = self.page.locator("input[type='password']").filter(visible=True)
        try:
            senhas_visiveis.first.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeout as exc:
            raise CasiErro("A tela de edição do usuário não abriu (campos de senha não apareceram).") from exc

        senha = self._campo_por_label(_rotulo(r"Senha"))
        confirmar = self._campo_por_label(_rotulo(r"Confirmar\s+senha"))
        if senha is None or confirmar is None:
            if senhas_visiveis.count() < 2:
                raise CasiErro("Não encontrei os campos 'Senha' e 'Confirmar senha'.")
            senha, confirmar = senhas_visiveis.nth(0), senhas_visiveis.nth(1)
        return senha, confirmar

    def _conferir_formulario(self, usuario: UsuarioCasi) -> None:
        """Garante que a tela de edição aberta é do usuário certo antes de mexer na senha."""
        campo_matricula = self._campo_por_label(_rotulo(r"Matr[ií]cula"))
        if campo_matricula is not None:
            valor = self._valor_campo(campo_matricula)
            if not mesma_matricula(valor, usuario.matricula):
                raise CasiErro(f"A tela de edição abriu outra matrícula ({valor}), esperado {usuario.matricula}.")
        else:
            log.warning("Não consegui ler a matrícula na tela de edição; usando a conferência feita na tabela.")

        campo_nome = self._campo_por_label(_rotulo(r"Nome"))
        if campo_nome is not None:
            valor = self._valor_campo(campo_nome)
            if normalizar_texto(valor) != normalizar_texto(usuario.nome):
                raise CasiErro(f"A tela de edição abriu outro nome ({valor}), esperado {usuario.nome}.")

    def _aguardar_confirmacao(self) -> None:
        """Espera a resposta do Salvar. Procura o texto em qualquer lugar visível (mensagem, popup, alerta)."""
        ja_utilizada = self.page.get_by_text(_SENHA_JA_UTILIZADA).filter(visible=True)
        sucesso = self.page.get_by_text(_SALVO_COM_SUCESSO).filter(visible=True)
        erros = self.page.locator(
            ".ui-messages-error, .ui-message-error, .ui-messages-fatal, .ui-messages-warn, .ui-message-warn, "
            ".ui-growl-item-container.ui-state-error"
        ).filter(visible=True)
        limite = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < limite:
            alertas = [a for a in self._alertas if a]
            reuso = [a for a in alertas if _SENHA_JA_UTILIZADA.search(a)]
            if ja_utilizada.count():
                reuso.append(" ".join(ja_utilizada.first.inner_text().split()))
            if reuso:
                raise SenhaJaUtilizada(reuso[0])
            if sucesso.count() or any(_SALVO_COM_SUCESSO.search(a) for a in alertas):
                return
            textos = [" ".join(t.split()) for t in erros.all_inner_texts() if t.strip()] + alertas
            if textos:
                raise GravacaoRecusada(" | ".join(textos))
            self.page.wait_for_timeout(300)
        raise CasiErro("Não apareceu 'Registro salvo com sucesso' depois de clicar em Salvar.")

    def resetar_senha(self, usuario: UsuarioCasi, nova_senha: str, simular: bool, nome_evidencia: str) -> None:
        """Clica no lápis do usuário (resultado da última pesquisa), grava a senha e salva."""
        atual = next((u for u in self._ler_resultados() if u.linha == usuario.linha), None)
        if atual is None or not mesma_matricula(atual.matricula, usuario.matricula):
            raise CasiErro("A tabela mudou desde a pesquisa; a linha não é mais do usuário esperado.")
        linha = self._tabela_usuarios().locator("tbody.ui-datatable-data > tr").nth(usuario.linha)

        # Só o lápis (editar). Nunca usar posição de coluna para não correr o risco de clicar no X (excluir).
        lapis = linha.locator(".fa-pencil, .ui-icon-pencil, [class*='pencil']")
        if not lapis.count():
            lapis = linha.locator(
                "a[title*='ditar' i], button[title*='ditar' i], a[title*='lterar' i], button[title*='lterar' i]"
            )
        if not lapis.count():
            raise CasiErro("Botão de edição (lápis) não encontrado na linha do usuário.")
        lapis.first.click()
        self._aguardar_ajax()

        campo_senha, campo_confirmar = self._campos_senha()
        self._conferir_formulario(usuario)

        campo_senha.fill(nova_senha)
        campo_confirmar.fill(nova_senha)
        if campo_senha.input_value() != nova_senha or campo_confirmar.input_value() != nova_senha:
            raise CasiErro("Os campos de senha não aceitaram o valor completo (limite de tamanho/máscara?).")

        if simular:
            self.evidencia(f"{nome_evidencia}_simulacao")
            log.info("[SIMULAÇÃO] Senha preenchida para %s - %s, mas NÃO salva.", usuario.matricula, usuario.nome)
            return

        self.page.evaluate(_JS_LIMPAR_MENSAGENS)
        self._alertas.clear()
        self._botao("Salvar").click()
        self._aguardar_ajax()
        try:
            self._aguardar_confirmacao()
        except (SenhaJaUtilizada, GravacaoRecusada):
            self.evidencia(f"{nome_evidencia}_recusada")
            raise
        self.evidencia(f"{nome_evidencia}_ok")
        log.info("Senha resetada no CASI para %s - %s.", usuario.matricula, usuario.nome)
