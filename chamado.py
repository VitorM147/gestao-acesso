"""Leitura do formulário do chamado e regras de negócio do reset de senha XCash."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher


class RevisaoManual(Exception):
    """O chamado não pode ser atendido automaticamente; um analista precisa tratar."""


class UsuarioNaoEncontrado(RevisaoManual):
    """A matrícula não existe no CASI."""


@dataclass(frozen=True)
class DadosChamado:
    nome: str
    matricula: str  # somente dígitos, exatamente como veio no chamado (pode ter zero à esquerda)


# Rótulos das perguntas do formulário do XCash (o valor vem depois dos dois-pontos)
_ROTULOS = {
    "nome": r"nome\s+completo[^:\n]*:",
    "matricula": r"matr[ií]cula[^:\n]*:",
}

# Partículas ignoradas na comparação de nomes
_PARTICULAS = {"de", "da", "do", "das", "dos", "e", "d"}

# Semelhança mínima para aceitar pequenas diferenças de digitação (ex.: Daniele x Danielle)
SIMILARIDADE_MINIMA = 0.92


def normalizar_texto(texto: str | None) -> str:
    """Minúsculas, sem acentos, espaços simples. Usado para comparações."""
    texto = texto or ""
    for _ in range(3):  # entidades HTML podem vir escapadas mais de uma vez
        novo = html.unescape(texto)
        if novo == texto:
            break
        texto = novo
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"\s*>\s*", " > ", texto)
    return re.sub(r"\s+", " ", texto).strip().casefold()


def html_para_texto(conteudo: str | None) -> str:
    """Converte o conteúdo do chamado (HTML, às vezes com entidades escapadas) em texto com uma pergunta por linha."""
    texto = conteudo or ""
    for _ in range(3):  # o GLPI pode devolver o HTML escapado mais de uma vez
        novo = html.unescape(texto)
        if novo == texto:
            break
        texto = novo
    texto = re.sub(r"(?i)<br\s*/?>", "\n", texto)
    texto = re.sub(r"(?i)</(p|div|li|tr|h\d|table)>", "\n", texto)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = texto.replace("\xa0", " ")
    # Garante quebra de linha antes de cada pergunta numerada: "4) ...", "5) ..."
    texto = re.sub(r"[ \t]+(?=\d{1,2}\)\s)", "\n", texto)
    linhas = (re.sub(r"[ \t]+", " ", linha).strip() for linha in texto.splitlines())
    return "\n".join(linha for linha in linhas if linha)


def _valor(linhas: list[str], rotulo: str) -> str | None:
    padrao = re.compile(rf"{rotulo}\s*(.*)$", re.IGNORECASE)
    for i, linha in enumerate(linhas):
        achou = padrao.search(linha)
        if achou:
            valor = achou.group(1).strip()
            if not valor and i + 1 < len(linhas):  # resposta na linha de baixo
                valor = linhas[i + 1].strip()
            return valor.strip(" .;-") or None
    return None


def validar_matricula(matricula_bruta: str | None) -> str:
    """Devolve só os dígitos da matrícula (mantendo zeros à esquerda) ou levanta RevisaoManual."""
    if not (matricula_bruta or "").strip():
        raise RevisaoManual("Matrícula não informada.")
    numero = re.search(r"\d[\d.\- ]*", matricula_bruta)
    digitos = re.sub(r"\D", "", numero.group()) if numero else ""
    if not digitos or int(digitos) == 0:
        raise RevisaoManual(f"Matrícula inválida: '{matricula_bruta}'.")
    if len(digitos) > 10:
        raise RevisaoManual(f"Matrícula com tamanho suspeito (CPF digitado no lugar?): '{matricula_bruta}'.")
    return digitos


def extrair_dados(conteudo_html: str) -> DadosChamado:
    """Lê nome e matrícula do formulário do XCash (como aparece na descrição do chamado)."""
    linhas = html_para_texto(conteudo_html).splitlines()
    valores = {campo: _valor(linhas, rotulo) for campo, rotulo in _ROTULOS.items()}

    if not valores["matricula"]:
        raise RevisaoManual("Não encontrei a matrícula no texto do chamado.")
    digitos = validar_matricula(valores["matricula"])
    if not valores["nome"]:
        raise RevisaoManual("Não encontrei o nome completo do colaborador no texto do chamado.")
    return DadosChamado(nome=valores["nome"], matricula=digitos)


def montar_dados(nome: str, matricula: str, descricao: str) -> DadosChamado:
    """Usa as colunas Nome/Matrícula da planilha; o que faltar é lido do texto da descrição."""
    nome, matricula = (nome or "").strip(), (matricula or "").strip()
    if (not nome or not matricula) and (descricao or "").strip():
        do_texto = extrair_dados(descricao)
        nome = nome or do_texto.nome
        matricula = matricula or do_texto.matricula
    if not nome:
        raise RevisaoManual("Nome do colaborador não informado.")
    return DadosChamado(nome=nome, matricula=validar_matricula(matricula))


def matricula_sem_zeros(matricula: str) -> str:
    digitos = re.sub(r"\D", "", matricula or "")
    if not digitos or int(digitos) == 0:
        raise ValueError(f"Matrícula inválida: '{matricula}'")
    return str(int(digitos))


def senha_padrao(matricula: str, caractere: str = "@") -> str:
    """Dpsp + caractere + matrícula sem zeros à esquerda. Ex.: (0126594, '#') -> Dpsp#126594"""
    return f"Dpsp{caractere}{matricula_sem_zeros(matricula)}"


def ler_caracteres(texto: str | None) -> list[str]:
    """Símbolos na ordem em que aparecem, sem repetir. '#!' -> ['#', '!']; 'Dpsp#126594' -> ['#']."""
    caracteres: list[str] = []
    for c in re.findall(r"[^\w\s]", texto or ""):
        if c not in caracteres:
            caracteres.append(c)
    return caracteres


def mesma_matricula(a: str | None, b: str | None) -> bool:
    da, db = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    return bool(da and db) and int(da) == int(db)


def _tokens(nome: str) -> list[str]:
    limpo = re.sub(r"[^a-z ]", " ", normalizar_texto(nome))
    return [t for t in limpo.split() if t not in _PARTICULAS]


def nomes_conferem(nome_chamado: str, nome_casi: str) -> tuple[bool, str]:
    """Confere se o nome do chamado corresponde ao do CASI. Retorna (confere, explicação)."""
    a, b = _tokens(nome_chamado), _tokens(nome_casi)
    if not a or not b:
        return False, "nome vazio"
    if a == b:
        return True, "nome idêntico"
    mesmo_primeiro_e_ultimo = a[0] == b[0] and a[-1] == b[-1]
    if mesmo_primeiro_e_ultimo and (set(a) <= set(b) or set(b) <= set(a)):
        return True, "mesmo primeiro e último nome (nome abreviado)"
    semelhanca = SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
    primeiro_parecido = SequenceMatcher(None, a[0], b[0]).ratio() >= 0.8  # Daniele x Danielle
    if primeiro_parecido and a[-1] == b[-1] and semelhanca >= SIMILARIDADE_MINIMA:
        return True, f"diferença mínima de digitação ({semelhanca:.0%} de semelhança)"
    return False, f"{semelhanca:.0%} de semelhança"
