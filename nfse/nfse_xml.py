"""Leitura das NFS-e autorizadas devolvidas pelo ISS Net.

Aceita o padrão nacional (desde 01/10/2026) e o ABRASF 2.04 (notas antigas), para conferência.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal

from .remessa import RE_BOLETO


@dataclass
class NotaAutorizada:
    formato: str  # "nacional" ou "abrasf"
    numero: str
    chave: str  # chave de acesso nacional (50 dígitos); vazia no ABRASF
    status: str
    serie_dps: str
    numero_dps: str
    documento_tomador: str
    nome_tomador: str
    valor_servico: Decimal
    valor_liquido: Decimal
    valor_iss: Decimal
    iss_retido: bool
    descricao: str
    emitida_em: str

    @property
    def boleto(self) -> str:
        achado = RE_BOLETO.search(self.descricao or "")
        return achado.group(0) if achado else ""


def remover_namespaces(raiz: ET.Element) -> ET.Element:
    for elemento in raiz.iter():
        if "}" in elemento.tag:
            elemento.tag = elemento.tag.split("}", 1)[1]
    return raiz


def _dec(texto: str | None) -> Decimal:
    return Decimal(texto) if texto else Decimal("0.00")


def _nacional(nfse: ET.Element) -> NotaAutorizada:
    inf = nfse.find("infNFSe")
    dps = inf.find("DPS/infDPS")
    toma = dps.find("toma")
    tp_ret = dps.findtext("valores/trib/tribMun/tpRetISSQN", "1")
    return NotaAutorizada(
        formato="nacional",
        numero=inf.findtext("nNFSe", ""),
        chave=(inf.get("Id") or "")[3:],  # tira o prefixo "NFS"
        status=inf.findtext("cStat", ""),
        serie_dps=dps.findtext("serie", ""),
        numero_dps=dps.findtext("nDPS", ""),
        documento_tomador=toma.findtext("CNPJ") or toma.findtext("CPF") or "",
        nome_tomador=toma.findtext("xNome", ""),
        valor_servico=_dec(dps.findtext("valores/vServPrest/vServ")),
        valor_liquido=_dec(inf.findtext("valores/vLiq")),
        valor_iss=_dec(inf.findtext("valores/vISSQN")),
        iss_retido=tp_ret in ("2", "3"),
        descricao=dps.findtext("serv/cServ/xDescServ", ""),
        emitida_em=inf.findtext("dhProc", ""),
    )


def _abrasf(comp: ET.Element) -> NotaAutorizada:
    inf = comp.find("Nfse/InfNfse")
    decl = inf.find("DeclaracaoPrestacaoServico/InfDeclaracaoPrestacaoServico")
    rps = decl.find("Rps/IdentificacaoRps")
    tomador = decl.find("TomadorServico")
    return NotaAutorizada(
        formato="abrasf",
        numero=inf.findtext("Numero", ""),
        chave="",
        status="100",
        serie_dps=rps.findtext("Serie", "") if rps is not None else "",
        numero_dps=rps.findtext("Numero", "") if rps is not None else "",
        documento_tomador=(
            tomador.findtext("IdentificacaoTomador/CpfCnpj/Cnpj")
            or tomador.findtext("IdentificacaoTomador/CpfCnpj/Cpf")
            or ""
        ),
        nome_tomador=tomador.findtext("RazaoSocial", ""),
        valor_servico=_dec(decl.findtext("Servico/Valores/ValorServicos")),
        valor_liquido=_dec(inf.findtext("ValoresNfse/ValorLiquidoNfse")),
        valor_iss=_dec(inf.findtext("ValoresNfse/ValorIss")),
        iss_retido=decl.findtext("Servico/IssRetido") == "1",
        descricao=decl.findtext("Servico/Discriminacao", ""),
        emitida_em=inf.findtext("DataEmissao", ""),
    )


def ler_nfse(conteudo: bytes, exigir: bool = True) -> list[NotaAutorizada]:
    """Lê um XML com uma ou mais NFS-e: consulta do ISS Net, resposta de emissão ou nota avulsa.

    No padrão nacional a tag é 'NFSe' na consulta e 'Nfse' na resposta de emissão; o que diferencia
    do ABRASF é o filho 'infNFSe' (nacional) ou 'InfNfse' (ABRASF).
    """
    # Nota de NFS-e não tem DOCTYPE nem ENTITY: recusar evita "bomba de entidades" em XML enviado pela tela.
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)", conteudo, re.IGNORECASE):
        raise ValueError("XML com DOCTYPE/ENTITY não é aceito")
    raiz = remover_namespaces(ET.fromstring(conteudo))
    notas = []
    for elemento in raiz.iter():
        if elemento.tag in ("NFSe", "Nfse") and elemento.find("infNFSe") is not None:
            notas.append(_nacional(elemento))
        elif elemento.tag == "CompNfse" and elemento.find("Nfse/InfNfse") is not None:
            notas.append(_abrasf(elemento))
    if exigir and not notas:
        raise ValueError("Nenhuma NFS-e encontrada no arquivo")
    return notas
