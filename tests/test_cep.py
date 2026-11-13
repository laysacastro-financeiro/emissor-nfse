"""Verificação de CEP: respostas simuladas do ViaCEP e do IBGE (nada vai à internet nos testes)."""
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nfse.cep as cep_mod  # noqa: E402
from nfse.cep import ServicoCep, formatar_cep, normalizar_cep, termos_de_busca  # noqa: E402

CANAA = "1502152"


def base_simulada(chamadas):
    def buscar(url):
        chamadas.append(url)
        if url.endswith("/68537000/json/"):
            return {"erro": "true"}  # CEP genérico que o ViaCEP não conhece (caso real: ALPHA CONSTRUCAO)
        if url.endswith("/30360430/json/"):
            return {"cep": "30360-430", "logradouro": "Rua Zodíaco", "bairro": "Santa Lúcia",
                    "localidade": "Belo Horizonte", "uf": "MG", "ibge": "3106200"}
        if url.endswith("/01001000/json/"):
            return {"cep": "01001-000", "localidade": "São Paulo", "uf": "SP", "ibge": "3550308"}
        if "localidades/municipios/3106200" in url:
            return {"id": 3106200, "nome": "Belo Horizonte"}
        if "localidades/municipios" in url:
            return {"id": int(CANAA), "nome": "Canaã dos Carajás"}
        if "viacep.com.br/ws/MG/" in url:
            return []  # nenhuma rua parecida em Belo Horizonte
        if "viacep.com.br/ws/PA/" in url:
            return [{"cep": "68354-217", "logradouro": "Rua São Bento", "bairro": "Paraiso das Águas",
                     "localidade": "Canaã dos Carajás", "uf": "PA", "ibge": CANAA},
                    {"cep": "68537-999", "logradouro": "Rua São Bento", "bairro": "Outro", "localidade": "Parauapebas",
                     "uf": "PA", "ibge": "1505536"}]
        raise AssertionError(url)
    return buscar


class TestCep(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chamadas = []
        self.servico = ServicoCep(buscar=base_simulada(self.chamadas), cache=Path(self.temp.name) / "c.json")
        self.pausa = mock.patch.object(cep_mod, "INTERVALO_ENTRE_CONSULTAS", 0)
        self.pausa.start()

    def tearDown(self):
        self.pausa.stop()
        self.temp.cleanup()

    def test_cep_que_confere(self):
        r = self.servico.verificar("30360-430", "3106200", "MG", "Rua Zodiaco", "Santa Lucia")
        self.assertEqual((r.status, r.cidade_do_cep), ("ok", "Belo Horizonte"))

    def test_cep_de_outra_cidade_e_divergente(self):
        r = self.servico.verificar("01001000", "3106200", "MG", "Rua Zodiaco", "Santa Lucia")
        self.assertEqual((r.status, r.cidade_do_cep), ("divergente", "São Paulo"))

    def test_cep_inexistente_sugere_o_correto_pela_rua(self):
        r = self.servico.verificar("68537000", CANAA, "PA", "Rua Sao Bento", "Paraiso das Aguas")
        self.assertEqual(r.status, "nao_encontrado")
        # Só entra CEP do próprio município do cadastro (o de Parauapebas é descartado).
        self.assertEqual([s["cep"] for s in r.sugestoes], ["68354217"])

    def test_busca_de_rua_pode_ficar_para_depois(self):
        r = self.servico.verificar("68537000", CANAA, "PA", "Rua Sao Bento", "Paraiso das Aguas", buscar_ruas=False)
        self.assertEqual((r.status, r.sugestoes, r.ruas_pendentes), ("nao_encontrado", [], True))
        self.assertFalse([u for u in self.chamadas if "viacep.com.br/ws/PA/" in u])  # a parte lenta não rodou
        r = self.servico.verificar("68537000", CANAA, "PA", "Rua Sao Bento", "Paraiso das Aguas")
        self.assertEqual(([s["cep"] for s in r.sugestoes], r.ruas_pendentes), (["68354217"], False))

    def test_usa_cache_e_nao_repete_consulta(self):
        self.servico.verificar("30360430", "3106200", "MG", "Rua Zodiaco")
        antes = len(self.chamadas)
        self.servico.verificar("30360430", "3106200", "MG", "Rua Zodiaco")
        self.assertEqual(len(self.chamadas), antes)

    def test_sem_internet_fica_indisponivel_e_nao_vai_para_o_cache(self):
        def cai(url):
            raise OSError("sem rede")
        servico = ServicoCep(buscar=cai, cache=Path(self.temp.name) / "x.json")
        self.assertEqual(servico.verificar("30360430", "3106200", "MG", "Rua Zodiaco").status, "indisponivel")
        self.assertFalse((Path(self.temp.name) / "x.json").exists())

    def test_brasilapi_e_a_primeira_fonte_e_viacep_confirma_o_que_ela_nao_acha(self):
        chamadas = []

        def buscar(url):
            chamadas.append(url)
            if "brasilapi" in url and url.endswith("/30360430"):
                return {"cep": "30360430", "state": "MG", "city": "Belo Horizonte", "neighborhood": "Santa Lúcia",
                        "street": "Rua Zodíaco", "ibge": {"city": "3106200"}}
            if "brasilapi" in url:
                raise urllib.error.HTTPError(url, 404, "nao achou", {}, None)
            return {"erro": True}  # ViaCEP: o CEP também não existe
        servico = ServicoCep(buscar=buscar, cache=Path(self.temp.name) / "r.json", rapido=True)
        self.assertEqual(servico.dados_do_cep("30360430")["cidade"], "Belo Horizonte")
        self.assertFalse([u for u in chamadas if "viacep" in u])  # achou na BrasilAPI: não consulta o ViaCEP
        self.assertEqual(servico.dados_do_cep("68537000"), {})  # BrasilAPI 404 -> ViaCEP confirma que não existe
        self.assertTrue([u for u in chamadas if "viacep" in u])

    def test_pre_carregar_consulta_so_cep_distinto_e_ainda_sem_cache(self):
        self.servico.dados_do_cep("30360430")  # já no cache
        antes = len(self.chamadas)
        self.servico.pre_carregar(["30360-430", "30360430", "01001000", "01001000"])
        self.assertEqual(len(self.chamadas) - antes, 1)  # só o 01001000
        self.servico.pre_carregar(["01001000"])
        self.assertEqual(len(self.chamadas) - antes, 1)

    def test_resposta_gzip_do_ibge(self):
        import gzip
        import io

        class Resposta(io.BytesIO):
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        corpo = gzip.compress('{"id": 1502152, "nome": "Canaã dos Carajás"}'.encode())
        with mock.patch("urllib.request.urlopen", return_value=Resposta(corpo)):
            self.assertEqual(ServicoCep._buscar_http("https://exemplo")["nome"], "Canaã dos Carajás")

    def test_formatos_e_termos(self):
        self.assertEqual((normalizar_cep("30.360-430"), formatar_cep("30360430")), ("30360430", "30360-430"))
        self.assertEqual(normalizar_cep("5300108"), "05300108")  # zero à esquerda perdido na exportação
        self.assertEqual(termos_de_busca("Rua Sao Bento 85"), ["Rua Sao Bento", "Sao Bento", "Sao"])
        self.assertEqual(termos_de_busca("AV . CONDESSA DO RIO NOVO1615")[:2], ["AV CONDESSA DO RIO NOVO", "CONDESSA DO RIO NOVO"])
        # Logradouro "sujo" do Gerência: número, loja, bairro e cidade colados na rua.
        self.assertEqual(termos_de_busca("R ARISTOBULO BARBOSA LEAO 500 LOJA 27 - MATA DA PRAIA - VITO")[:2],
                         ["R ARISTOBULO BARBOSA LEAO", "ARISTOBULO BARBOSA LEAO"])
        self.assertEqual(termos_de_busca("Av. Rene FreyNo 598 Sala 2, 89580000 Sala 2")[0], "Av Rene FreyNo")
        self.assertEqual(termos_de_busca("R"), [])

    def test_municipio_do_cadastro_errado(self):
        # Rua e bairro do cadastro existem no município do CEP: o provável erro é o município do cadastro.
        confere = ServicoCep.municipio_do_cep_confere_com_endereco
        self.assertTrue(confere({"logradouro": "Rua Manoel Pasqual", "bairro": "Monte Pasqual"},
                                "RUA MANOEL PASQUAL", "MONTE PASQUAL"))
        self.assertTrue(confere({"logradouro": "Rua Pastor Laechler", "bairro": "Santo Inácio"}, "R PT LAECHLER", "STO INACIO"))
        self.assertTrue(confere({"logradouro": "Avenida Presidente Epitácio Pessoa", "bairro": "Estados"},
                                "AV PRESIDENTE EPITACIO PESSOA", "ESTADOS"))
        # CEP de uma rua que não tem nada a ver com o endereço: não oferece trocar o município.
        self.assertFalse(confere({"logradouro": "Rua Lagoa Azul", "bairro": "Prolar"},
                                 "R ARISTOBULO BARBOSA LEAO 500", "MATA DA PRAIA"))
        self.assertFalse(confere({}, "Rua A", "B"))


if __name__ == "__main__":
    unittest.main()
