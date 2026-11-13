"""Consulta do cadastro do CNPJ na Receita Federal (endereço cadastrado, inclusive o CEP).

A página oficial do cartão CNPJ (solucoes.receita.fazenda.gov.br/Servicos/cnpjreva) exige captcha e não pode
ser automatizada. Os mesmos dados cadastrais são publicados pela própria Receita como dados abertos e servidos
sem captcha por serviços públicos: BrasilAPI (principal) e Minha Receita (reserva), que usam a mesma base.

Cuidados:
- a base aberta é atualizada periodicamente: alteração cadastral muito recente pode não aparecer;
- só há dados de CNPJ (pessoa jurídica): CPF não tem consulta;
- só o CNPJ da venda sai do computador, e só para vendas com problema de CEP.
"""
from __future__ import annotations

import re
import time
import urllib.error
from pathlib import Path

import config

from .cep import buscar_json, _trava as _trava_cache

ARQUIVO_CACHE = config.DADOS_DIR / "cache_cnpj.json"
FONTES = ("https://brasilapi.com.br/api/cnpj/v1/{cnpj}", "https://minhareceita.org/{cnpj}")
VALIDADE_ENCONTRADO = 30 * 86400
VALIDADE_NAO_ENCONTRADO = 2 * 86400
INTERVALO_ENTRE_CONSULTAS = 0.3


def so_digitos(texto: str) -> str:
    return re.sub(r"\D", "", texto or "")


class ServicoCnpj:
    def __init__(self, buscar=None, cache: Path | None = None, agora=time.time):
        self._buscar = buscar or buscar_json
        self._arquivo = Path(cache or ARQUIVO_CACHE)
        self._agora = agora
        self.consultas_feitas = 0

    def _ler(self) -> dict:
        with _trava_cache:
            return __import__("json").loads(self._arquivo.read_text("utf-8")) if self._arquivo.exists() else {}

    def _gravar(self, cnpj: str, valor: dict) -> None:
        import json

        with _trava_cache:
            cache = json.loads(self._arquivo.read_text("utf-8")) if self._arquivo.exists() else {}
            cache[cnpj] = {"dados": valor, "em": self._agora()}
            self._arquivo.parent.mkdir(parents=True, exist_ok=True)
            temporario = self._arquivo.with_suffix(".tmp")
            temporario.write_text(json.dumps(cache, ensure_ascii=False), "utf-8")
            temporario.replace(self._arquivo)

    @staticmethod
    def normalizar(resposta: dict) -> dict:
        tipo = (resposta.get("descricao_tipo_de_logradouro") or "").strip()
        rua = (resposta.get("logradouro") or "").strip()
        return {
            "cep": so_digitos(str(resposta.get("cep") or "")).zfill(8) if resposta.get("cep") else "",
            "logradouro": f"{tipo} {rua}".strip() if rua else "",
            "numero": (resposta.get("numero") or "").strip(),
            "bairro": (resposta.get("bairro") or "").strip(),
            "municipio": (resposta.get("municipio") or "").strip(),
            "uf": (resposta.get("uf") or "").strip(),
            "ibge": str(resposta.get("codigo_municipio_ibge") or ""),
            "razao_social": (resposta.get("razao_social") or "").strip(),
            "situacao": (resposta.get("descricao_situacao_cadastral") or "").strip(),
        }

    def consultar(self, cnpj: str) -> dict | None:
        """Cadastro do CNPJ na Receita: dict normalizado; {} se o CNPJ não existe na base; None se indisponível."""
        cnpj = so_digitos(cnpj)
        if len(cnpj) != 14:
            return {}  # CPF (11 dígitos) ou documento inválido: não há consulta
        item = self._ler().get(cnpj)
        if item:
            validade = VALIDADE_ENCONTRADO if item["dados"] else VALIDADE_NAO_ENCONTRADO
            if self._agora() - item["em"] <= validade:
                return item["dados"]
        for modelo in FONTES:
            self.consultas_feitas += 1
            time.sleep(INTERVALO_ENTRE_CONSULTAS)
            try:
                resposta = self._buscar(modelo.format(cnpj=cnpj))
            except urllib.error.HTTPError as erro:
                if erro.code in (400, 404):  # CNPJ inexistente na base: não adianta tentar a outra fonte
                    self._gravar(cnpj, {})
                    return {}
                continue  # 429 (limite de uso) ou 5xx: tenta a fonte de reserva
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                continue
            if isinstance(resposta, dict) and resposta.get("cnpj"):
                dados = self.normalizar(resposta)
                self._gravar(cnpj, dados)
                return dados
        return None
