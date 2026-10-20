"""Resposta automática do chamado no GLPI (web), reaproveitando uma sessão já autenticada.

Por que sessão salva: o GLPI do DPSP aceita login por usuário/senha e também SSO da Microsoft
(com MFA). Automatizar SSO/MFA é frágil, então o fluxo é:
  1. Uma vez: "python main.py --login-glpi" abre o navegador, você loga normalmente (inclusive SSO)
     e a automação grava o cookie em glpi_session.json.
  2. Depois: a automação reaproveita esse cookie para abrir o chamado e responder.
Quando a sessão expira, a automação avisa para refazer o login.
"""
from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Error as PlaywrightError, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

log = logging.getLogger("xcash.glpi")


class GlpiWebErro(Exception):
    """Falha ao responder o chamado (a senha no CASI já foi tratada; dá para tentar de novo depois)."""


class PrecisaLoginGlpi(GlpiWebErro):
    """Não há sessão válida; rode 'python main.py --login-glpi'."""


def texto_para_html(texto: str) -> str:
    """Converte a mensagem em parágrafos simples para o editor do GLPI."""
    linhas = (texto or "").splitlines()
    return "".join(f"<p>{html.escape(l.strip())}</p>" if l.strip() else "<p>&nbsp;</p>" for l in linhas) or "<p></p>"


class GlpiWeb:
    def __init__(self, contexto: BrowserContext, base_url: str, timeout_ms: int, pasta_evidencias: Path):
        self.contexto = contexto
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = timeout_ms
        self.pasta_evidencias = pasta_evidencias
        self.page: Page = contexto.pages[0] if contexto.pages else contexto.new_page()
        self.page.set_default_timeout(timeout_ms)

    def url_chamado(self, numero: str) -> str:
        return f"{self.base_url}/front/ticket.form.php?id={numero}"

    # ---------- sessão ----------
    def _na_tela_de_login(self) -> bool:
        if "login" in self.page.url or self.page.url.rstrip("/").endswith("index.php"):
            return True
        return self.page.locator("#login_name, input[name='login_name']").count() > 0

    def abrir_chamado(self, numero: str) -> None:
        self.page.goto(self.url_chamado(numero), wait_until="domcontentloaded")
        self.page.wait_for_timeout(800)
        if self._na_tela_de_login():
            raise PrecisaLoginGlpi(
                "A sessão do GLPI expirou ou não existe. Rode 'python main.py --login-glpi' e faça o login."
            )

    def evidencia(self, nome: str) -> Path | None:
        if not self.pasta_evidencias:
            return None
        try:
            pasta = self.pasta_evidencias / datetime.now().strftime("%Y-%m-%d")
            pasta.mkdir(parents=True, exist_ok=True)
            caminho = pasta / f"{datetime.now():%H%M%S}_glpi_{re.sub(r'[^0-9A-Za-z_-]', '_', nome)}.png"
            self.page.screenshot(path=str(caminho), full_page=True)
            return caminho
        except PlaywrightError as exc:
            log.warning("Não consegui salvar print do GLPI: %s", exc)
            return None

    # ---------- resposta ----------
    def responder(self, numero: str, mensagem: str, status_solucao: str, submeter: bool) -> str:
        """Abre o chamado, escreve a solução com a mensagem, marca o tipo como 'Solucionado' e,
        se submeter, clica em Adicionar. Levanta GlpiWebErro em caso de falha."""
        self.abrir_chamado(numero)
        form = self._abrir_formulario_solucao()
        self._preencher_editor(form, texto_para_html(mensagem))
        self._definir_tipo_solucao(form, status_solucao)

        if not submeter:
            self.evidencia(f"{numero}_solucao_preenchida")
            return (f"Solução preenchida no GLPI (mensagem + tipo '{status_solucao}'), mas NÃO enviada "
                    "(GLPI_SUBMETER desligado / simulação).")

        form.locator("button[type='submit'][name='add']").first.click()
        self.page.wait_for_timeout(1500)
        if not self._confirmar_envio(form):
            self.evidencia(f"{numero}_pos_envio")
            raise GlpiWebErro("Cliquei em Adicionar, mas não confirmei que a solução foi registrada.")
        self.evidencia(f"{numero}_solucionado")
        return "Chamado respondido e solução registrada no GLPI."

    def _abrir_formulario_solucao(self):
        """Espera o rodapé da linha do tempo, clica em 'Solução' e devolve o <form> da solução."""
        botao = self.page.get_by_role("button", name=re.compile(r"solu[çc]", re.IGNORECASE))
        try:
            botao.first.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeout as exc:
            raise GlpiWebErro("Não achei o botão 'Solução' no chamado (a linha do tempo não carregou?).") from exc
        form = self.page.locator("form:has(select[name='solutiontypes_id'])")
        if not (form.count() and form.first.is_visible()):
            botao.first.scroll_into_view_if_needed()
            botao.first.click()
        try:
            form.first.wait_for(state="visible", timeout=self.timeout_ms)
            form.first.locator("iframe.tox-edit-area__iframe").first.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeout as exc:
            raise GlpiWebErro("A tela de Solução não abriu no GLPI.") from exc
        return form.first

    def _preencher_editor(self, form, html_conteudo: str) -> None:
        """Escreve a mensagem no editor TinyMCE da solução, pela API do TinyMCE (sincroniza no textarea)."""
        iframe = form.locator("iframe.tox-edit-area__iframe").first
        iframe.wait_for(state="visible", timeout=self.timeout_ms)
        frame_id = iframe.get_attribute("id") or ""
        editor_id = frame_id[:-4] if frame_id.endswith("_ifr") else frame_id
        ok = self.page.evaluate(
            """({id, html}) => {
                const ed = window.tinymce && window.tinymce.get(id);
                if (ed) { ed.setContent(html); ed.save(); return true; }
                return false;
            }""",
            {"id": editor_id, "html": html_conteudo},
        )
        if not ok:  # fallback: escreve direto no corpo do iframe
            corpo = iframe.content_frame.locator("body")
            corpo.click()
            corpo.evaluate("(el, html) => { el.innerHTML = html; el.dispatchEvent(new Event('input', {bubbles:true})); }",
                           html_conteudo)

    def _definir_tipo_solucao(self, form, status: str) -> None:
        """Marca o campo obrigatório (etiqueta) da solução como 'Solucionado'. As opções vêm por AJAX."""
        container = form.locator("select[name='solutiontypes_id']").locator(
            "xpath=following::*[contains(@class,'select2-selection')][1]")
        if not container.count():
            raise GlpiWebErro("Não encontrei o campo de tipo de solução no GLPI.")
        if status.lower() in (container.first.inner_text() or "").strip().lower():
            return  # já está em 'Solucionado'
        container.first.click()
        busca = self.page.locator("input.select2-search__field")
        try:  # se houver campo de busca, digita para filtrar
            busca.wait_for(state="visible", timeout=4000)
            busca.fill(status)
        except PlaywrightError:
            pass
        # As opções chegam por AJAX e costumam demorar alguns segundos: espera a opção aparecer.
        alvo = self.page.locator("li.select2-results__option").filter(
            has_text=re.compile(re.escape(status), re.IGNORECASE))
        try:
            alvo.first.wait_for(state="visible", timeout=self.timeout_ms)
        except PlaywrightTimeout:
            self.page.keyboard.press("Escape")
            raise GlpiWebErro(f"A opção '{status}' não apareceu no campo de tipo de solução do GLPI "
                              "(esperei o carregamento e não veio).")
        alvo.first.click()
        self.page.wait_for_timeout(300)

    def _confirmar_envio(self, form) -> bool:
        """Deu certo quando o formulário da solução fecha (o GLPI recarrega a linha do tempo) e não há erro."""
        limite = time.monotonic() + self.timeout_ms / 1000
        while time.monotonic() < limite:
            erros = self.page.locator(".alert-danger, .toast-danger, .glpi-toast-error, .error_single").filter(visible=True)
            if erros.count():
                texto = " ".join(" ".join(t.split()) for t in erros.all_inner_texts() if t.strip())
                raise GlpiWebErro(f"O GLPI recusou a solução: {texto[:200]}")
            try:
                if not form.is_visible():
                    return True
            except PlaywrightError:
                return True  # o formulário sumiu do DOM (recarregou) = enviado
            self.page.wait_for_timeout(500)
        try:
            return not form.is_visible()
        except PlaywrightError:
            return True


def login_interativo(contexto: BrowserContext, base_url: str, timeout_ms: int) -> None:
    """Abre o GLPI e espera você terminar o login (inclusive SSO). Como o perfil é persistente, o login
    fica guardado em disco e nas próximas vezes a janela já abre logada. Loga a URL para acompanhar."""
    from urllib.parse import urlparse

    base_url = base_url.rstrip("/")
    host = urlparse(base_url).netloc
    page = contexto.pages[0] if contexto.pages else contexto.new_page()
    page.goto(f"{base_url}/front/central.php", wait_until="domcontentloaded")
    log.info("Faça o login NESTA janela do navegador (pode usar o SSO). Aguardando concluir...")

    limite = time.monotonic() + 300  # até 5 minutos para o login manual
    ultimo = ""
    while time.monotonic() < limite:
        time.sleep(1.5)  # não usa a página, para não quebrar se ela estiver navegando/fechada
        try:
            if page.is_closed():
                raise GlpiWebErro("A janela do navegador foi fechada antes de concluir o login. "
                                  "Rode de novo e conclua o login NA janela que a automação abrir.")
            url = page.url
            if url != ultimo:
                log.info("Janela do GLPI está em: %s", url)
                ultimo = url
            tem_form_login = page.locator("#login_name, input[name='login_name']").count() > 0
            em_sso = "login.microsoftonline" in url or "/oauth" in url.lower() or "/saml" in url.lower()
            if host in url and not tem_form_login and not em_sso:
                page.goto(f"{base_url}/front/central.php", wait_until="domcontentloaded")  # página estável
                time.sleep(0.8)
                if page.locator("#login_name, input[name='login_name']").count() > 0:
                    continue  # ainda caiu no login; segue esperando
                log.info("GLPI logado. O perfil foi guardado; nas próximas vezes a janela já abre logada.")
                return
        except GlpiWebErro:
            raise
        except PlaywrightError as exc:
            if "closed" in str(exc).lower():
                raise GlpiWebErro("A janela/navegador foi fechada antes de concluir o login. "
                                  "Rode de novo e conclua o login na janela que abrir.") from exc
            continue  # página navegando
    raise GlpiWebErro("Tempo esgotado esperando o login no GLPI.")
