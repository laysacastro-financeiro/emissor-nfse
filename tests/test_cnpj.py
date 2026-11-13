"""Cadastro do CNPJ na Receita Federal usado na verificação de CEP (respostas simuladas; sem internet)."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nfse.cep as cep_mod  # noqa: E402
import nfse.cnpj as cnpj_mod  # noqa: E402
from nfse.cep import ServicoCep  # noqa: E402
from nfse.cnpj import ServicoCnpj  # noqa: E402

CNPJ = "12345678000199"
VITORIA = "3205309"
SERRA = "3205002"
RUAS: list = []  # resposta da busca por rua no ViaCEP (cada teste define a sua)


def receita(cep, ibge, municipio, uf, rua, bairro, tipo="RUA"):
    return {"cnpj": CNPJ, "cep": cep, "codigo_municipio_ibge": int(ibge), "municipio": municipio, "uf": uf,
            "descricao_tipo_de_logradouro": tipo, "logradouro": rua, "bairro": bairro, "numero": "10",
            "razao_social": "EMPRESA TESTE", "descricao_situacao_cadastral": "ATIVA"}


def montar(dados_receita, chamadas):
    """ViaCEP, IBGE e Receita simulados. `dados_receita`: dict da resposta da BrasilAPI (ou exceção)."""
    def buscar(url):
        chamadas.append(url)
        if "brasilapi" in url or "minhareceita" in url:
            if isinstance(dados_receita, Exception):
                raise dados_receita
            return dados_receita
        if url.endswith("/29066300/json/"):
            return {"cep": "29066-300", "logradouro": "Rua das Gaivotas", "bairro": "Jardim Camburi",
                    "localidade": "Vitória", "uf": "ES", "ibge": VITORIA}
        if url.endswith("/29000000/json/"):
            return {"erro": "true"}
        if url.endswith("/29160000/json/"):
            return {"cep": "29160-000", "localidade": "Serra", "uf": "ES", "ibge": SERRA}
        if "localidades/municipios" in url:
            return {"id": int(VITORIA), "nome": "Vitória"}
        if "viacep.com.br/ws/ES/" in url:
            return RUAS
        raise AssertionError(url)
    return buscar


class TestCnpj(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chamadas = []
        self.pausas = [mock.patch.object(cep_mod, "INTERVALO_ENTRE_CONSULTAS", 0),
                       mock.patch.object(cnpj_mod, "INTERVALO_ENTRE_CONSULTAS", 0)]
        for p in self.pausas:
            p.start()

    def tearDown(self):
        for p in self.pausas:
            p.stop()
        self.temp.cleanup()

    def servicos(self, resposta):
        buscar = montar(resposta, self.chamadas)
        cnpj = ServicoCnpj(buscar=buscar, cache=Path(self.temp.name) / "cnpj.json")
        return ServicoCep(buscar=buscar, cache=Path(self.temp.name) / "cep.json", cnpj=cnpj)

    def test_cep_da_receita_corrige_automaticamente_quando_a_rua_confere(self):
        servico = self.servicos(receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        r = servico.verificar("29000000", VITORIA, "ES", "Rua das Gaivotas", "Jardim Camburi", CNPJ)
        self.assertEqual(r.status, "nao_encontrado")
        self.assertEqual(r.cep_automatico, "29066300")
        self.assertEqual(r.sugestoes[0]["cep"], "29066300")
        self.assertEqual(r.sugestoes[0]["origem"], "receita")
        self.assertTrue(r.receita_consultada)

    def test_rua_diferente_so_sugere_sem_aplicar(self):
        servico = self.servicos(receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        r = servico.verificar("29000000", VITORIA, "ES", "Avenida Fernando Ferrari", "Goiabeiras", CNPJ)
        self.assertEqual(r.cep_automatico, "")
        self.assertFalse(r.sugestoes[0]["mesma_rua"])

    def test_cep_da_receita_que_os_correios_nao_conhecem_nao_e_sugerido(self):
        servico = self.servicos(receita("29000-000", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        r = servico.verificar("29000000", VITORIA, "ES", "Rua das Gaivotas", "", CNPJ)
        self.assertEqual((r.cep_automatico, r.sugestoes), ("", []))

    def test_receita_confirma_que_o_municipio_do_cadastro_esta_errado(self):
        servico = self.servicos(receita("29160-000", SERRA, "SERRA", "ES", "DAS GAIVOTAS", "CENTRO"))
        r = servico.verificar("29160000", VITORIA, "ES", "Rua das Gaivotas", "Centro", CNPJ)
        self.assertEqual(r.status, "divergente")
        self.assertEqual(r.alternativa_municipio["ibge"], SERRA)
        self.assertTrue(r.alternativa_municipio["confirmado_receita"])
        self.assertEqual(r.cep_automatico, "")  # município nunca é trocado sem a equipe confirmar

    def test_endereco_da_receita_em_outra_cidade_e_oferecido(self):
        servico = self.servicos(receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        # Cadastro do Gerência em outro município (Serra), CEP inexistente: a Receita aponta Vitória.
        r = servico.verificar("29000000", SERRA, "ES", "Via Universitaria", "CIA I", CNPJ)
        self.assertEqual(r.endereco_receita["cidade"], "Vitória")
        self.assertEqual((r.endereco_receita["cep"], r.endereco_receita["ibge"]), ("29066300", VITORIA))
        self.assertEqual(r.cep_automatico, "")

    def test_cep_geral_oferece_os_ceps_da_rua_da_receita(self):
        servico = self.servicos(receita("29160-000", SERRA, "SERRA", "ES", "DAS GAIVOTAS", "CENTRO"))
        rua = [{"cep": "29160-111", "logradouro": "Rua das Gaivotas", "bairro": "Centro", "ibge": SERRA}]
        with mock.patch.object(sys.modules[__name__], "RUAS", rua):
            r = servico.verificar("29160000", SERRA, "ES", "R GAIVOTAS", "Centro", CNPJ)
        self.assertEqual((r.status, r.cep_geral), ("geral", True))
        self.assertEqual((r.sugestoes[0]["cep"], r.sugestoes[0]["origem"]), ("29160111", "receita_rua"))

    def test_cep_geral_sem_cep_de_rua_continua_ok(self):
        servico = self.servicos(receita("29160-000", SERRA, "SERRA", "ES", "DAS GAIVOTAS", "CENTRO"))
        r = servico.verificar("29160000", SERRA, "ES", "R GAIVOTAS", "Centro", CNPJ)
        self.assertEqual((r.status, r.cep_geral, r.sugestoes), ("ok", True, []))

    def test_cep_correto_nao_consulta_a_receita(self):
        servico = self.servicos(receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        r = servico.verificar("29066300", VITORIA, "ES", "Rua das Gaivotas", "", CNPJ)
        self.assertEqual(r.status, "ok")
        self.assertFalse([u for u in self.chamadas if "brasilapi" in u])

    def test_cpf_nao_consulta_a_receita(self):
        servico = self.servicos(receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        r = servico.verificar("29000000", VITORIA, "ES", "Rua das Gaivotas", "", "12345678901")
        self.assertFalse(r.receita_consultada)
        self.assertFalse([u for u in self.chamadas if "brasilapi" in u])

    def test_receita_fora_do_ar_nao_marca_como_consultada(self):
        servico = self.servicos(urllib.error.URLError("sem rede"))
        r = servico.verificar("29000000", VITORIA, "ES", "Rua das Gaivotas", "", CNPJ)
        self.assertFalse(r.receita_consultada)
        self.assertEqual(r.sugestoes, [])

    def test_usa_a_segunda_fonte_quando_a_primeira_cai(self):
        resposta = receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI")

        def buscar(url):
            if "brasilapi" in url:
                raise urllib.error.HTTPError(url, 429, "limite", {}, None)
            return resposta
        servico = ServicoCnpj(buscar=buscar, cache=Path(self.temp.name) / "cnpj.json")
        self.assertEqual(servico.consultar(CNPJ)["cep"], "29066300")

    def test_cnpj_inexistente_na_base_fica_no_cache(self):
        def buscar(url):
            raise urllib.error.HTTPError(url, 404, "nao existe", {}, None)
        servico = ServicoCnpj(buscar=buscar, cache=Path(self.temp.name) / "cnpj.json")
        self.assertEqual(servico.consultar(CNPJ), {})
        self.assertEqual(servico.consultas_feitas, 1)
        self.assertEqual(servico.consultar(CNPJ), {})
        self.assertEqual(servico.consultas_feitas, 1)

    def test_consulta_repetida_usa_o_cache(self):
        servico = self.servicos(receita("29066-300", VITORIA, "VITORIA", "ES", "DAS GAIVOTAS", "JARDIM CAMBURI"))
        servico.cnpj.consultar(CNPJ)
        servico.cnpj.consultar(CNPJ)
        self.assertEqual(servico.cnpj.consultas_feitas, 1)


if __name__ == "__main__":
    unittest.main()
