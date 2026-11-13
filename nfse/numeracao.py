"""Numeração das DPS por série.

Um pedido do Gerência nunca recebe dois números: se a DPS for gerada de novo (reprocessamento,
falha do ISS Net), o pedido reaproveita o número que já tinha. Isso evita nota em dobro.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class RegistroDps:
    def __init__(self, caminho: Path, serie: str):
        self.caminho = Path(caminho)
        self.serie = serie
        self._trava = threading.Lock()

    def _ler(self) -> dict:
        if self.caminho.exists():
            dados = json.loads(self.caminho.read_text("utf-8"))
            if dados["serie"] != self.serie:
                raise ValueError(f"Registro {self.caminho.name} é da série {dados['serie']}, não {self.serie}")
            return dados
        return {"serie": self.serie, "ultimo_numero": 0, "pedidos": {}}

    def _gravar(self, dados: dict) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario = self.caminho.with_suffix(".tmp")
        temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=1), "utf-8")
        temporario.replace(self.caminho)

    def numeros_para(self, pedidos: list[str]) -> dict[str, int]:
        """Devolve o número da DPS de cada pedido, reservando números novos só para pedidos inéditos."""
        with self._trava:
            dados = self._ler()
            novos = False
            for pedido in pedidos:
                if pedido not in dados["pedidos"]:
                    dados["ultimo_numero"] += 1
                    dados["pedidos"][pedido] = dados["ultimo_numero"]
                    novos = True
            if novos:
                self._gravar(dados)
            return {p: dados["pedidos"][p] for p in pedidos}

    def reservar(self, disponiveis: list[int], pedidos: list[str]) -> dict[str, int]:
        """Dá a cada pedido sem número um dos números `disponiveis` (informados pelo ISS Net), em ordem,
        pulando números já usados por outro pedido. Pedidos que já têm número ficam como estão.
        Devolve só as novas reservas."""
        with self._trava:
            dados = self._ler()
            usados = set(dados["pedidos"].values()) | {int(n) for n in dados.get("descartados", {})}
            livres = [n for n in sorted(set(int(x) for x in disponiveis)) if n not in usados]
            novas = {}
            for pedido in pedidos:
                if pedido in dados["pedidos"]:
                    continue
                if not livres:
                    break
                novas[pedido] = livres.pop(0)
            if novas:
                dados["pedidos"].update(novas)
                dados["ultimo_numero"] = max([dados["ultimo_numero"], *novas.values()])
                self._gravar(dados)
            return novas

    def numeros_usados(self) -> set[int]:
        with self._trava:
            dados = self._ler()
            return set(dados["pedidos"].values()) | {int(n) for n in dados.get("descartados", {})}

    def descartar(self, pedido: str, motivo: str) -> int | None:
        """O número deste pedido já pertence a outra NFS-e no ISS Net (E0014): tira o pedido dele e nunca mais o
        oferece. O pedido volta a ficar sem número, para receber outro dos disponíveis do ISS Net."""
        with self._trava:
            dados = self._ler()
            numero = dados["pedidos"].pop(pedido, None)
            if numero is not None:
                dados.setdefault("descartados", {})[str(numero)] = {"pedido": pedido, "motivo": motivo}
                self._gravar(dados)
            return numero

    def pedido_do_numero(self, numero: int) -> str | None:
        with self._trava:
            pedidos = self._ler()["pedidos"]
        return next((p for p, n in pedidos.items() if n == int(numero)), None)

    def ja_numerados(self, pedidos: list[str]) -> list[str]:
        with self._trava:
            existentes = self._ler()["pedidos"]
        return [p for p in pedidos if p in existentes]
