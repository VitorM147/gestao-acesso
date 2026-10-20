"""Leitura do CSV exportado da lista de chamados do GLPI (colunas ID, Categoria, Status, Descrição...)."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path

from chamado import RevisaoManual, extrair_dados, normalizar_texto
from planilha import normalizar_cabecalho

# Chamados com esses status não são importados
STATUS_ENCERRADOS = ("solucionado", "fechado", "solved", "closed")

_COLUNAS = {
    "chamado": ("id", "chamado", "ticket", "n chamado", "numero"),
    "categoria": ("categoria",),
    "status": ("status",),
    "descricao": ("descricao", "conteudo"),
}


class CsvErro(Exception):
    pass


@dataclass(frozen=True)
class ChamadoCsv:
    chamado: str  # só dígitos ("2 319 697" -> "2319697")
    categoria: str
    status: str
    descricao: str
    nome: str  # tirado do formulário na descrição ("" se não achou)
    matricula: str

    @property
    def encerrado(self) -> bool:
        return normalizar_texto(self.status).startswith(STATUS_ENCERRADOS)


def _ler_texto(caminho: Path) -> str:
    dados = caminho.read_bytes()
    for codificacao in ("utf-8-sig", "cp1252"):  # GLPI exporta em UTF-8; o Excel salva em cp1252
        try:
            return dados.decode(codificacao)
        except UnicodeDecodeError:
            continue
    return dados.decode("latin-1")


def mascarar_cpf(descricao: str) -> str:
    """Troca o valor das linhas de CPF por *** (o CPF não é usado pela automação)."""
    return re.sub(r"(?im)^([^:\n]*\bcpf\b[^:\n]*:)[^\n]*$", r"\1 ***", descricao or "")


def ler_csv(caminho: Path) -> list[ChamadoCsv]:
    try:
        texto = _ler_texto(caminho)
    except PermissionError as exc:
        raise CsvErro(f"Não consegui abrir {caminho.name}. Feche o arquivo no Excel e rode de novo.") from exc
    primeira_linha = texto.split("\n", 1)[0]
    separador = max((";", "\t", ","), key=primeira_linha.count)
    leitor = csv.DictReader(io.StringIO(texto, newline=""), delimiter=separador)

    colunas: dict[str, str] = {}
    for original in leitor.fieldnames or []:
        titulo = normalizar_cabecalho(original)
        for campo, nomes in _COLUNAS.items():
            if campo not in colunas and titulo in nomes:
                colunas[campo] = original
    faltando = [c for c in ("chamado", "categoria", "descricao") if c not in colunas]
    if faltando:
        raise CsvErro(
            f"O arquivo {caminho.name} não tem as colunas {', '.join(faltando)}. Na lista de chamados do GLPI, "
            "inclua as colunas ID, Categoria e Descrição antes de exportar."
        )

    chamados = []
    for linha in leitor:
        descricao = linha.get(colunas["descricao"]) or ""
        try:
            dados = extrair_dados(descricao)
            nome, matricula = dados.nome, dados.matricula
        except RevisaoManual:
            nome = matricula = ""  # a linha vai para revisão manual com o motivo na hora de processar
        chamados.append(ChamadoCsv(
            chamado=re.sub(r"\D", "", linha.get(colunas["chamado"]) or ""),
            categoria=(linha.get(colunas["categoria"]) or "").strip(),
            status=(linha.get(colunas.get("status", "")) or "").strip(),
            descricao=descricao,
            nome=nome,
            matricula=matricula,
        ))
    return chamados
