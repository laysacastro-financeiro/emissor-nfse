"""Conferência das notas autorizadas contra os pedidos da remessa.

Trava contra o erro de "nota trocada": uma nota só vai para o retorno do Gerência se o tomador,
o valor e o boleto baterem com o pedido.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import config

from .documentos import normalizar_documento
from .nfse_xml import NotaAutorizada
from .numeracao import RegistroDps
from .remessa import Remessa, Venda


@dataclass
class Resultado:
    nota: NotaAutorizada
    venda: Venda | None
    forma_associacao: str
    divergencias: list[str] = field(default_factory=list)

    @property
    def apta(self) -> bool:
        """Pode entrar no arquivo de retorno."""
        return self.venda is not None and not self.divergencias


def conferir(venda: Venda, nota: NotaAutorizada) -> list[str]:
    divergencias = []
    if normalizar_documento(nota.documento_tomador) != venda.cliente.documento:
        divergencias.append(
            f"Tomador da nota ({nota.documento_tomador}) diferente do cliente do pedido ({venda.cliente.documento})"
        )
    if nota.valor_servico != venda.valor_total:
        divergencias.append(f"Valor da nota R$ {nota.valor_servico} diferente do pedido R$ {venda.valor_total}")
    if venda.boleto:
        if not nota.boleto:
            divergencias.append(f"Boleto {venda.boleto} não aparece na descrição da nota")
        elif nota.boleto != venda.boleto:
            divergencias.append(f"Boleto da nota ({nota.boleto}) diferente do pedido ({venda.boleto})")
    if nota.status != "100":
        divergencias.append(f"Nota com situação {nota.status} (esperado 100 - autorizada)")
    return divergencias


def _localizar(
    nota: NotaAutorizada, remessa: Remessa, registro: RegistroDps | None, serie: str | None = None
) -> tuple[Venda | None, str]:
    # 1) Nota emitida por este sistema: a série/número da DPS identifica o pedido sem ambiguidade.
    if registro is not None and nota.serie_dps == (serie or config.SERIE_DPS) and nota.numero_dps:
        pedido = registro.pedido_do_numero(int(nota.numero_dps))
        if pedido and remessa.venda(pedido):
            return remessa.venda(pedido), "número da DPS"
    # 2) Boleto citado na descrição.
    if nota.boleto:
        candidatas = [v for v in remessa.vendas if v.boleto == nota.boleto]
        if len(candidatas) == 1:
            return candidatas[0], "boleto"
    # 3) Cliente + valor, só se houver um único pedido possível.
    documento = normalizar_documento(nota.documento_tomador)
    candidatas = [
        v for v in remessa.vendas if v.cliente.documento == documento and v.valor_total == nota.valor_servico
    ]
    if len(candidatas) == 1:
        return candidatas[0], "cliente + valor"
    return None, "não localizado"


def associar(
    remessa: Remessa, notas: list[NotaAutorizada], registro: RegistroDps | None = None, serie: str | None = None
) -> list[Resultado]:
    resultados = []
    for nota in notas:
        venda, forma = _localizar(nota, remessa, registro, serie)
        resultado = Resultado(nota=nota, venda=venda, forma_associacao=forma)
        if venda is None:
            resultado.divergencias.append("Nota não corresponde a nenhum pedido deste lote")
        else:
            resultado.divergencias.extend(conferir(venda, nota))
        if not nota.chave:
            resultado.divergencias.append("Nota sem chave de acesso nacional (formato antigo) — não entra no retorno")
        resultados.append(resultado)

    contagem = Counter(r.venda.pedido for r in resultados if r.venda is not None)
    for r in resultados:
        if r.venda is not None and contagem[r.venda.pedido] > 1:
            r.divergencias.append(f"Pedido {r.venda.pedido} associado a {contagem[r.venda.pedido]} notas")
    return resultados
