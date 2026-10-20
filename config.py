"""Carrega as configurações do arquivo .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from chamado import ler_caracteres

BASE_DIR = Path(__file__).resolve().parent
ARQUIVO_ENV = BASE_DIR / ".env"

_CAMINHO_XCASH = "Tecnologia > Operações de TI > Sistemas > Acessos/Liberações > Acesso ao Sistema XCash (Tesouraria loja)"
CATEGORIA_PADRAO = f"{_CAMINHO_XCASH} > Reset de senha"
CATEGORIA_CRIACAO_PADRAO = f"{_CAMINHO_XCASH} > Criação de usuário XCash"


@dataclass(frozen=True)
class Config:
    casi_url: str
    casi_usuario: str
    casi_senha: str

    planilha: Path
    csv_glpi: Path | None  # CSV exportado do GLPI; chamados novos dele entram na planilha a cada execução
    exigir_categoria: bool
    categoria_id: str  # ID da categoria de Reset de senha (2314030)
    categoria_nome: str  # Reset de senha
    categoria_criacao: str  # Criação de usuário XCash
    categoria_criacao_id: str
    # Chamado de criação cujo usuário já existe no CASI: "resetar" (envia acesso) ou "revisao" (manual)
    criacao_se_ja_existe: str

    # Caracteres tentados entre "Dpsp" e a matrícula, em ordem, quando o CASI recusa a senha
    caracteres_senha: tuple[str, ...]

    # GLPI (resposta automática do chamado pela web)
    glpi_url: str
    glpi_perfil: Path  # pasta do perfil do navegador (guarda o login do GLPI entre execuções)
    glpi_navegador: str  # navegador do GLPI: chrome, msedge ou chromium
    responder_glpi: bool  # liga/desliga a resposta automática no GLPI
    glpi_submeter: bool  # trava de segurança: se False, preenche a solução mas NÃO clica em Adicionar
    glpi_status_solucao: str  # status a marcar na solução (ex.: "Solucionado")

    pausa_entre_chamados_s: int  # pausa entre um chamado e outro, para não sobrecarregar GLPI/CASI

    navegador: str
    headless: bool
    timeout_ms: int


def _texto(nome: str, padrao: str = "") -> str:
    return (os.getenv(nome) or padrao).strip()


def _booleano(nome: str, padrao: bool) -> bool:
    valor = _texto(nome)
    if not valor:
        return padrao
    return valor.lower() in ("1", "true", "sim", "s", "yes", "y")


def _inteiro(nome: str, padrao: int) -> int:
    valor = _texto(nome)
    try:
        return int(valor) if valor else padrao
    except ValueError as exc:
        raise SystemExit(f"Valor inválido para {nome} no .env: '{valor}' (esperado número inteiro).") from exc


def _caminho(valor: str) -> Path:
    caminho = Path(valor)
    return caminho if caminho.is_absolute() else BASE_DIR / caminho


def carregar() -> Config:
    if not ARQUIVO_ENV.exists():
        raise SystemExit(f"Arquivo .env não encontrado em {BASE_DIR}. Copie .env.example para .env e preencha.")
    load_dotenv(ARQUIVO_ENV, override=True)

    faltando = [n for n in ("CASI_URL", "CASI_USUARIO", "CASI_SENHA") if not _texto(n)]
    if faltando:
        raise SystemExit("Preencha no arquivo .env: " + ", ".join(faltando))

    caracteres = ler_caracteres(os.getenv("CARACTERES_SENHA") or "@!#$&*")
    if not caracteres:
        raise SystemExit("CARACTERES_SENHA no .env precisa ter pelo menos um símbolo (ex.: '@!#$&*').")

    criacao_se_ja_existe = _texto("CRIACAO_SE_JA_EXISTE", "resetar").lower()
    if criacao_se_ja_existe not in ("resetar", "revisao"):
        raise SystemExit("CRIACAO_SE_JA_EXISTE no .env deve ser 'resetar' ou 'revisao'.")

    csv_glpi = _texto("CSV_GLPI", "glpi.csv")

    return Config(
        casi_url=_texto("CASI_URL").rstrip("/"),
        casi_usuario=_texto("CASI_USUARIO"),
        casi_senha=os.getenv("CASI_SENHA") or "",
        planilha=_caminho(_texto("PLANILHA", "chamados.xlsx")),
        csv_glpi=_caminho(csv_glpi) if csv_glpi else None,
        exigir_categoria=_booleano("EXIGIR_CATEGORIA", True),
        categoria_id=_texto("CATEGORIA_ID", "2314030"),
        # CATEGORIA_NOME é o nome antigo da opção; continua valendo
        categoria_nome=_texto("CATEGORIA_RESET") or _texto("CATEGORIA_NOME", CATEGORIA_PADRAO),
        categoria_criacao=_texto("CATEGORIA_CRIACAO", CATEGORIA_CRIACAO_PADRAO),
        categoria_criacao_id=_texto("CATEGORIA_CRIACAO_ID"),
        criacao_se_ja_existe=criacao_se_ja_existe,
        caracteres_senha=tuple(caracteres),
        glpi_url=_texto("GLPI_URL", "https://suporte.dpsp.io").rstrip("/"),
        glpi_perfil=_caminho(_texto("GLPI_PERFIL", "perfil_glpi")),
        glpi_navegador=_texto("GLPI_NAVEGADOR", "chrome").lower(),
        responder_glpi=_booleano("RESPONDER_GLPI", False),
        glpi_submeter=_booleano("GLPI_SUBMETER", False),
        glpi_status_solucao=_texto("GLPI_STATUS_SOLUCAO", "Solucionado"),
        pausa_entre_chamados_s=max(0, _inteiro("PAUSA_ENTRE_CHAMADOS", 5)),
        navegador=_texto("NAVEGADOR", "msedge").lower(),
        headless=_booleano("HEADLESS", True),
        timeout_ms=_inteiro("TIMEOUT_SEGUNDOS", 30) * 1000,
    )
