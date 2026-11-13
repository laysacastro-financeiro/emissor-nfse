"""Telas e regras de uso compartilhado: login, CSRF, lotes de N em N, exclusão e histórico de retorno.

Roda numa pasta de dados temporária — não toca nos lotes reais.
"""
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as emissor  # noqa: E402
import autenticacao  # noqa: E402
import config  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


class TestWeb(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        dados = Path(self.temp.name)
        self.patches = [
            mock.patch.object(config, "DADOS_DIR", dados),
            mock.patch.object(config, "PASTA_RETORNO_GERENCIA", dados / "retornos_gerencia"),
            mock.patch.object(emissor, "LOTES_DIR", dados / "lotes"),
            mock.patch.object(emissor, "LIXEIRA_DIR", dados / "lixeira"),
            mock.patch.object(config, "VERIFICAR_CEPS_AO_IMPORTAR", False),  # teste não abre fio em segundo plano
            mock.patch.object(autenticacao, "ARQUIVO_USUARIOS", dados / "usuarios.json"),
            mock.patch.object(autenticacao, "ARQUIVO_AUDITORIA", dados / "auditoria.log"),
        ]
        from nfse.cep import ResultadoCep
        # Nenhum teste fala com a internet: todo CEP é "ok", salvo quando o teste troca a verificação.
        self.patches.append(mock.patch.object(emissor.servico_cep, "verificar", return_value=ResultadoCep("ok")))
        self.patches.append(mock.patch.object(emissor.servico_cep, "pre_carregar"))
        for p in self.patches:
            p.start()
        emissor._registros.clear()
        self.cliente = emissor.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        emissor._registros.clear()
        self.temp.cleanup()

    def csrf(self, url="/"):
        pagina = self.cliente.get(url, follow_redirects=True).data.decode()
        return re.search(r"campo\.value = '([0-9a-f]+)'", pagina).group(1)

    def entrar_como_admin(self):
        token = self.csrf("/primeiro-acesso")
        resposta = self.cliente.post("/primeiro-acesso", data={
            "nome": "Usuario Teste", "login": "usuario.teste", "senha": "senha-forte-1",
            "confirmacao": "senha-forte-1", "csrf_token": token})
        self.assertEqual(resposta.status_code, 302)

    def importar_remessa(self):
        token = self.csrf()
        resposta = self.cliente.post("/importar", data={
            "remessa": (io.BytesIO((FIX / "remessa_28092026_10notas.txt").read_bytes()), "remessa.txt"),
            "csrf_token": token}, content_type="multipart/form-data")
        return resposta.headers["Location"].rsplit("/", 1)[1]

    def criar_lote(self, origem, **dados):
        token = self.csrf(f"/lote/{origem}")
        resposta = self.cliente.post(f"/lote/{origem}/criar-lote-producao", data={**dados, "csrf_token": token})
        return resposta.headers["Location"].rsplit("/", 1)[1]

    def test_sem_usuario_pede_primeiro_acesso_e_depois_login(self):
        self.assertIn("/primeiro-acesso", self.cliente.get("/").headers["Location"])
        self.entrar_como_admin()
        self.assertEqual(self.cliente.get("/").status_code, 200)
        self.cliente.post("/sair", data={"csrf_token": self.csrf()})
        self.assertIn("/entrar", self.cliente.get("/").headers["Location"])

    def test_login_com_senha_errada_falha(self):
        self.entrar_como_admin()
        self.cliente.post("/sair", data={"csrf_token": self.csrf()})
        token = self.csrf("/entrar")
        self.cliente.post("/entrar", data={"login": "usuario.teste", "senha": "errada", "csrf_token": token})
        self.assertIn("/entrar", self.cliente.get("/").headers["Location"])

    def test_post_sem_token_e_recusado(self):
        self.entrar_como_admin()
        self.assertEqual(self.cliente.post("/sair").status_code, 400)

    def test_lotes_de_n_em_n_tiram_as_vendas_da_remessa(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        primeiro = self.criar_lote(origem, modo="proximas", quantidade="3")
        segundo = self.criar_lote(origem, modo="proximas", quantidade="3")
        _, r1, amb1 = emissor.carregar(primeiro)
        _, r2, _ = emissor.carregar(segundo)
        p1, p2 = {v.pedido for v in r1.vendas}, {v.pedido for v in r2.vendas}
        self.assertEqual((len(p1), len(p2), amb1), (3, 3, "producao"))
        self.assertFalse(p1 & p2)  # nenhuma venda em dois lotes
        pagina = self.cliente.get(f"/lote/{origem}").data.decode()
        self.assertIn("Restam <b>4</b> vendas prontas", pagina)
        for pedido in p1 | p2:
            self.assertNotIn(f"<td>{pedido}</td>", pagina)  # sumiram da lista da remessa
        resto = self.criar_lote(origem, modo="todas")
        _, r3, _ = emissor.carregar(resto)
        self.assertEqual(len(r3.vendas), 4)

    def test_excluir_lote_sem_nota_e_bloquear_lote_com_nota(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        lote = self.criar_lote(origem, modo="proximas", quantidade="2")
        (emissor.LOTES_DIR / lote / "notas").mkdir()
        (emissor.LOTES_DIR / lote / "notas" / "nfse_1.xml").write_bytes(b"<x/>")
        self.cliente.post(f"/lote/{lote}/excluir", data={"csrf_token": self.csrf()})
        self.assertTrue((emissor.LOTES_DIR / lote).exists())  # com nota: não exclui
        self.cliente.post(f"/lote/{origem}/excluir", data={"csrf_token": self.csrf()})
        self.assertFalse((emissor.LOTES_DIR / origem).exists())
        self.assertEqual(len(list(emissor.LIXEIRA_DIR.iterdir())), 1)
        self.assertIn("excluiu o lote", autenticacao.ARQUIVO_AUDITORIA.read_text("utf-8"))

    def test_verificar_ceps_bloqueia_sugere_e_aplica(self):
        from nfse.cep import ResultadoCep
        self.entrar_como_admin()
        origem = self.importar_remessa()
        _, remessa, _ = emissor.carregar(origem)
        ruim = remessa.vendas[0]
        cep_ruim = ruim.cliente.cep

        def verificar(cep, ibge, uf, logradouro, bairro="", documento="", buscar_ruas=True):
            if cep == cep_ruim:
                return ResultadoCep("nao_encontrado", sugestoes=[{"cep": "68354217", "logradouro": "Rua A", "bairro": "B"}])
            return ResultadoCep("ok")

        with mock.patch.object(emissor.servico_cep, "verificar", side_effect=verificar):
            token = self.csrf(f"/lote/{origem}")
            self.cliente.post(f"/lote/{origem}/verificar-ceps", data={"csrf_token": token})
        _, remessa, _ = emissor.carregar(origem)
        venda = remessa.venda(ruim.pedido)
        self.assertFalse(venda.pronta)  # CEP incompatível bloqueia a emissão
        self.assertTrue(any(e.startswith("CEP não confere com o município") for e in venda.erros))
        self.assertEqual(venda.sugestoes_cep[0]["cep"], "68354217")
        self.assertEqual(len(remessa.prontas), len(remessa.vendas) - 1)
        csv = self.cliente.get(f"/lote/{origem}/ceps.csv").data.decode("utf-8-sig")
        self.assertIn("68354-217", csv)
        # Só CEPs sugeridos pela verificação podem ser aplicados.
        self.cliente.post(f"/lote/{origem}/aplicar-cep", data={"pedido": ruim.pedido, "cep": "11111111",
                                                              "csrf_token": self.csrf(f"/lote/{origem}")})
        self.assertEqual(emissor.carregar(origem)[1].venda(ruim.pedido).cliente.cep, cep_ruim)
        self.cliente.post(f"/lote/{origem}/aplicar-cep", data={"pedido": ruim.pedido, "cep": "68354217",
                                                              "csrf_token": self.csrf(f"/lote/{origem}")})
        venda = emissor.carregar(origem)[1].venda(ruim.pedido)
        self.assertEqual((venda.cliente.cep, venda.pronta), ("68354217", True))
        self.assertIn("aplicou o CEP", autenticacao.ARQUIVO_AUDITORIA.read_text("utf-8"))
        # O CEP corrigido acompanha a venda para o lote de produção.
        lote = self.criar_lote(origem, modo="todas")
        self.assertEqual(emissor.carregar(lote)[1].venda(ruim.pedido).cliente.cep, "68354217")

    def test_importar_confere_os_ceps_em_segundo_plano(self):
        self.entrar_como_admin()
        with mock.patch.object(config, "VERIFICAR_CEPS_AO_IMPORTAR", True):
            origem = self.importar_remessa()
            emissor._verificacoes_em_andamento[origem].join(30)
        _, remessa, _ = emissor.carregar(origem)
        feitas = emissor.ler_json(emissor.LOTES_DIR / origem / "cep.json")
        self.assertEqual(len(feitas), len(remessa.vendas))
        self.assertFalse(emissor.verificando_ceps(origem))

    def test_cep_manual_e_endereco_da_receita_corrigem_cep_e_municipio(self):
        from nfse.cep import ResultadoCep
        self.entrar_como_admin()
        origem = self.importar_remessa()
        _, remessa, _ = emissor.carregar(origem)
        ruim, outra = remessa.vendas[0], remessa.vendas[1]
        cep_ruim = ruim.cliente.cep
        curitiba = {"cep": "81030415", "ibge": "4106902", "cidade": "Curitiba", "uf": "PR",
                    "logradouro": "AV PRESIDENTE WENCESLAU BRAZ", "numero": "3765", "bairro": "NOVO MUNDO"}

        def verificar(cep, ibge, uf, logradouro, bairro="", documento="", buscar_ruas=True):
            if cep == cep_ruim:
                return ResultadoCep("nao_encontrado", endereco_receita=curitiba)
            return ResultadoCep("ok")

        with mock.patch.object(emissor.servico_cep, "verificar", side_effect=verificar), \
                mock.patch.object(emissor.servico_cep, "nome_do_municipio", return_value="Cidade do cadastro"):
            self.cliente.post(f"/lote/{origem}/verificar-ceps", data={"csrf_token": self.csrf(f"/lote/{origem}")})
            pagina = self.cliente.get(f"/lote/{origem}").data.decode()
            self.assertIn("Endereço do CNPJ na Receita Federal", pagina)
            self.assertIn("Digitar o CEP manualmente", pagina)
            # Pedido sem endereço da Receita não pode usar a rota.
            self.cliente.post(f"/lote/{origem}/aplicar-endereco-receita", data={
                "pedido": outra.pedido, "csrf_token": self.csrf(f"/lote/{origem}")})
            self.assertNotEqual(emissor.carregar(origem)[1].venda(outra.pedido).cliente.cep, "81030415")
            self.cliente.post(f"/lote/{origem}/aplicar-endereco-receita", data={
                "pedido": ruim.pedido, "csrf_token": self.csrf(f"/lote/{origem}")})
        venda = emissor.carregar(origem)[1].venda(ruim.pedido)
        self.assertEqual((venda.cliente.cep, venda.cliente.cod_municipio, venda.cliente.uf, venda.pronta),
                         ("81030415", "4106902", "PR", True))
        self.assertIn("endereço do CNPJ na Receita Federal", autenticacao.ARQUIVO_AUDITORIA.read_text("utf-8"))

        # CEP digitado: precisa existir nos Correios; o município passa a ser o do CEP.
        dados_cep = {"ibge": "3550308", "cidade": "São Paulo", "uf": "SP", "logradouro": "Praça da Sé", "bairro": "Sé"}
        with mock.patch.object(emissor.servico_cep, "dados_do_cep", side_effect=lambda c: {} if c == "00000000" else dados_cep):
            for cep in ("123", "00000000"):
                self.cliente.post(f"/lote/{origem}/corrigir-cep", data={
                    "pedido": ruim.pedido, "cep": cep, "csrf_token": self.csrf(f"/lote/{origem}")})
                self.assertEqual(emissor.carregar(origem)[1].venda(ruim.pedido).cliente.cep, "81030415")
            self.cliente.post(f"/lote/{origem}/corrigir-cep", data={
                "pedido": ruim.pedido, "cep": "01001-000", "csrf_token": self.csrf(f"/lote/{origem}")})
        venda = emissor.carregar(origem)[1].venda(ruim.pedido)
        self.assertEqual((venda.cliente.cep, venda.cliente.cod_municipio, venda.cliente.uf), ("01001000", "3550308", "SP"))
        correcao = emissor.ler_json(emissor.LOTES_DIR / origem / "correcoes_cep.json")[ruim.pedido]
        self.assertEqual(correcao["de"], cep_ruim)  # o valor original do Gerência não se perde
        # Nova remessa com o mesmo cliente e o mesmo cadastro errado: a correção já vem aplicada.
        segunda = self.importar_remessa()
        v2 = emissor.carregar(segunda)[1].venda(ruim.pedido)
        self.assertEqual((v2.cliente.cep, v2.cliente.cod_municipio, v2.cliente.uf), ("01001000", "3550308", "SP"))
        self.assertTrue(any("correção já feita por" in a for a in v2.avisos))

    def test_painel_de_remessas_mostra_completas_pendentes_e_arquivadas(self):
        import json
        self.entrar_como_admin()
        origem = self.importar_remessa()
        pagina = self.cliente.get("/").data.decode()
        self.assertIn("Precisam de atenção (1)", pagina)
        self.assertIn("Remessas completas (0)", pagina)
        self.assertIn("Ainda não emitida", pagina)
        # Emite tudo (simulado): lote de produção com todas as vendas autorizadas e retorno gerado.
        lote = self.criar_lote(origem, modo="todas")
        pasta = emissor.LOTES_DIR / lote
        _, remessa, _ = emissor.carregar(lote)
        envios = {v.pedido: {"pedido": v.pedido, "numero_dps": 100 + i, "situacao": "autorizada",
                             "numero_nfse": str(500 + i), "mensagens": [], "atualizado_em": "", "serie": "3"}
                  for i, v in enumerate(remessa.vendas)}
        (pasta / "envios.json").write_text(json.dumps(envios), "utf-8")
        painel = emissor.painel_remessas()
        linha = painel["atencao"][0]
        self.assertEqual((linha["situacao"], linha["emitidas"], linha["retornadas"], linha["sem_retorno"]),
                         ("retorno", len(remessa.vendas), 0, len(remessa.vendas)))
        self.assertIn("sem arquivo de retorno", self.cliente.get("/").data.decode())
        (pasta / "retornos.json").write_text(json.dumps([{"arquivo": "x.txt", "modo": "todas", "notas": [
            e["numero_nfse"] for e in envios.values()]}]), "utf-8")
        self.assertEqual(emissor.painel_remessas()["completas"][0]["situacao"], "completa")
        self.assertIn("Remessas completas (1)", self.cliente.get("/").data.decode())
        # Rejeitada pelo ISS Net volta a pedir atenção, com o lote onde está.
        pedido = remessa.vendas[0].pedido
        envios[pedido].update(situacao="rejeitada", numero_nfse="", mensagens=["E0240 - CEP"])
        (pasta / "envios.json").write_text(json.dumps(envios), "utf-8")
        linha = emissor.painel_remessas()["atencao"][0]
        self.assertEqual((linha["situacao"], linha["rejeitadas"], linha["lotes_com_problema"]), ("erro", 1, [lote]))
        # Arquivar tira da lista de pendências; desarquivar devolve.
        self.cliente.post(f"/lote/{origem}/arquivar", data={"motivo": "teste", "csrf_token": self.csrf("/")})
        painel = emissor.painel_remessas()
        self.assertEqual((len(painel["atencao"]), len(painel["arquivadas"])), (0, 1))
        self.assertIn("Remessas arquivadas (1)", self.cliente.get("/").data.decode())
        self.cliente.post(f"/lote/{origem}/desarquivar", data={"csrf_token": self.csrf("/")})
        self.assertEqual(len(emissor.painel_remessas()["atencao"]), 1)

    def test_seletor_de_paginas_mostra_numeros_e_reticencias(self):
        self.assertEqual(emissor.numeros_de_pagina(1, 1), [1])
        self.assertEqual(emissor.numeros_de_pagina(1, 8), [1, 2, 3, None, 8])
        self.assertEqual(emissor.numeros_de_pagina(4, 8), [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(emissor.numeros_de_pagina(8, 20), [1, None, 6, 7, 8, 9, 10, None, 20])
        self.entrar_como_admin()
        origem = self.importar_remessa()
        with mock.patch.object(emissor, "POR_PAGINA", 3):
            pagina = self.cliente.get(f"/lote/{origem}?pagina=2").data.decode()
        self.assertIn('aria-current="page">2<', pagina)
        self.assertIn('class="numero-pagina"', pagina)
        self.assertEqual(pagina.count('aria-label="Páginas"'), 2)  # acima e abaixo da tabela

    def test_nota_recusada_por_e0240_com_cep_ok_permite_digitar_outro_cep(self):
        import json
        self.entrar_como_admin()
        origem = self.importar_remessa()
        venda = emissor.carregar(origem)[1].vendas[0]
        self.assertFalse(venda.cep_corrigivel)  # CEP que confere: sem campo manual
        mensagem = "E0240 - O CEP informado para o endereço nacional do tomador do serviço não existe ..."
        (emissor.LOTES_DIR / origem / "envios.json").write_text(json.dumps({venda.pedido: {
            "pedido": venda.pedido, "numero_dps": 1, "situacao": "rejeitada", "numero_nfse": "",
            "mensagens": [mensagem], "atualizado_em": "", "serie": "3"}}), "utf-8")
        venda = emissor.carregar(origem)[1].venda(venda.pedido)
        self.assertTrue(venda.cep_corrigivel)  # a caixa de CEP manual aparece na nota rejeitada
        self.assertTrue(venda.pronta)  # segue podendo ser reenviada depois de corrigir

    def test_bairro_nao_informado_pode_ser_corrigido_na_tela_e_a_correcao_fica_para_o_cliente(self):
        from nfse.remessa import decodificar
        self.entrar_como_admin()
        origem = self.importar_remessa()
        arquivo = emissor.LOTES_DIR / origem / "remessa.txt"
        linhas = decodificar(arquivo.read_bytes()).splitlines()
        primeira = next(i for i, linha in enumerate(linhas) if linha.startswith("C;"))
        campos = linhas[primeira].split(";")
        campos[11] = ""  # bairro em branco no cadastro do Gerência
        linhas[primeira] = ";".join(campos)
        arquivo.write_bytes(("\r\n".join(linhas) + "\r\n").encode("utf-8"))
        venda = next(v for v in emissor.carregar(origem)[1].vendas if v.cliente.documento == campos[1])
        self.assertIn("Bairro não informado", venda.erros)
        self.assertFalse(venda.pronta)
        self.assertTrue(venda.endereco_corrigivel)
        self.assertIn("Corrigir o endereço do cliente", self.cliente.get(f"/lote/{origem}").data.decode())

        token = self.csrf(f"/lote/{origem}")
        dados = {"pedido": venda.pedido, "logradouro": venda.cliente.logradouro, "numero": venda.cliente.numero,
                 "complemento": venda.cliente.complemento, "csrf_token": token}
        self.cliente.post(f"/lote/{origem}/corrigir-endereco", data={**dados, "bairro": ""})  # bairro continua vazio
        self.assertIn("Bairro não informado", emissor.carregar(origem)[1].venda(venda.pedido).erros)
        self.cliente.post(f"/lote/{origem}/corrigir-endereco", data={**dados, "bairro": "  Centro  "})
        corrigida = emissor.carregar(origem)[1].venda(venda.pedido)
        self.assertEqual(corrigida.cliente.bairro, "Centro")
        self.assertNotIn("Bairro não informado", corrigida.erros)
        self.assertTrue(corrigida.pronta)
        self.assertTrue(any("Endereço corrigido (bairro)" in a for a in corrigida.avisos))
        # o painel de remessas também enxerga a venda como apta
        self.assertTrue(dict(emissor.vendas_do_lote(emissor.LOTES_DIR / origem))[venda.pedido])
        # a correção vale para o cliente: outra remessa com o mesmo cadastro em branco já sai corrigida
        remessa_nova = emissor.ler_remessa(arquivo.read_bytes())
        self.assertIn("Bairro não informado", remessa_nova.venda(venda.pedido).erros)
        emissor.aplicar_endereco(remessa_nova)
        self.assertTrue(remessa_nova.venda(venda.pedido).pronta)
        # se o Gerência corrigir o cadastro (texto diferente do original), a correção deixa de ser aplicada
        campos[11] = "Asa Sul"
        linhas[primeira] = ";".join(campos)
        arquivo.write_bytes(("\r\n".join(linhas) + "\r\n").encode("utf-8"))
        gerencia = emissor.carregar(origem)[1].venda(venda.pedido)
        self.assertEqual(gerencia.cliente.bairro, "Asa Sul")

    def test_nota_recusada_por_em059_oferece_iss_retido_e_a_dps_sai_com_retencao(self):
        import json
        from lxml import etree
        from nfse.dps import montar_dps
        self.entrar_como_admin()
        origem = self.importar_remessa()
        venda = emissor.carregar(origem)[1].vendas[0]
        self.assertFalse(venda.retencao_pendente or venda.iss_retido)
        mensagem = "EM059 - A retenção é obrigatória para substitutos tributários. (Correção: Altere tpRetISSQN para 2)"
        (emissor.LOTES_DIR / origem / "envios.json").write_text(json.dumps({venda.pedido: {
            "pedido": venda.pedido, "numero_dps": 1, "situacao": "rejeitada", "numero_nfse": "",
            "mensagens": [mensagem], "atualizado_em": "", "serie": "3"}}), "utf-8")
        venda = emissor.carregar(origem)[1].venda(venda.pedido)
        self.assertTrue(venda.retencao_pendente)
        self.assertIn("Emitir com ISS retido pelo tomador", self.cliente.get(f"/lote/{origem}").data.decode())

        def retencao(v):
            raiz = etree.fromstring(montar_dps(v, "3", 1, iss_retido=v.iss_retido))
            return raiz.find(".//{*}tribMun/{*}tpRetISSQN").text, raiz.find(".//{*}vServ").text

        self.assertEqual(retencao(venda)[0], "1")
        token = self.csrf(f"/lote/{origem}")
        self.cliente.post(f"/lote/{origem}/iss-retido", data={"pedido": venda.pedido, "csrf_token": token})
        marcada = emissor.carregar(origem)[1].venda(venda.pedido)
        self.assertTrue(marcada.iss_retido and not marcada.retencao_pendente)
        self.assertTrue(any("ISS retido pelo tomador" in a for a in marcada.avisos))
        self.assertTrue(marcada.pronta)
        self.assertEqual(retencao(marcada), ("2", f"{marcada.valor_total:.2f}"))  # valor da nota continua integral
        # vale para o cliente em qualquer remessa, e dá para desfazer
        self.cliente.post(f"/lote/{origem}/iss-retido",
                          data={"pedido": venda.pedido, "desfazer": "1", "csrf_token": token})
        self.assertFalse(emissor.carregar(origem)[1].venda(venda.pedido).iss_retido)

    def test_seguranca_login_bloqueia_tentativas_e_primeiro_acesso_so_no_proprio_computador(self):
        autenticacao._falhas_login.clear()
        # sem usuários, quem chega por outro endereço não cria o administrador
        self.assertEqual(self.cliente.get("/primeiro-acesso", environ_base={"REMOTE_ADDR": "10.0.0.5"}).status_code, 403)
        self.entrar_como_admin()
        self.cliente.post("/sair", data={"csrf_token": self.csrf()})
        for _ in range(autenticacao.TENTATIVAS_LOGIN):
            self.cliente.post("/entrar", data={"login": "usuario.teste", "senha": "errada", "csrf_token": self.csrf("/entrar")})
        pagina = self.cliente.post("/entrar", data={"login": "usuario.teste", "senha": "senha-forte-1",
                                                    "csrf_token": self.csrf("/entrar")}).data.decode()
        self.assertIn("Muitas tentativas", pagina)  # nem a senha certa entra durante o bloqueio
        self.assertIn("/entrar", self.cliente.get("/").headers["Location"])
        autenticacao._falhas_login.clear()

    def test_seguranca_cabecalhos_texto_js_xml_e_segredo_aleatorio(self):
        import json
        from nfse.nfse_xml import ler_nfse
        import segredos
        resposta = self.cliente.get("/primeiro-acesso")
        self.assertEqual(resposta.headers["X-Frame-Options"], "DENY")
        self.assertEqual(resposta.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(emissor._texto_para_js("a'b<\"\\"), "a\\u0027b\\u003c\\u0022\\u005c")
        with self.assertRaises(ValueError):
            ler_nfse(b'<!DOCTYPE x [<!ENTITY a "b">]><x>&a;</x>')
        primeiro = segredos.segredo("teste_segredo", "VARIAVEL_QUE_NAO_EXISTE")
        self.assertEqual(primeiro, segredos.segredo("teste_segredo", "VARIAVEL_QUE_NAO_EXISTE"))
        self.assertRegex(primeiro, r"^[0-9a-f]{64}$")
        # nome de arquivo com aspas não vira código na página inicial
        self.entrar_como_admin()
        token = self.csrf()
        self.cliente.post("/importar", data={
            "remessa": (io.BytesIO((FIX / "remessa_28092026_10notas.txt").read_bytes()), "a'b\"x.txt"),
            "csrf_token": token}, content_type="multipart/form-data")
        pagina = self.cliente.get("/").data.decode()
        self.assertNotIn("(a'b", pagina)
        self.assertIn("a\\u0027b", pagina)

    def rejeitar_por_em030(self, lote, pedido):
        import json
        (emissor.LOTES_DIR / lote / "envios.json").write_text(json.dumps({pedido: {
            "pedido": pedido, "numero_dps": 1, "situacao": "rejeitada", "numero_nfse": "",
            "mensagens": ["EM030 - CNPJ do tomador do serviço está vinculado a mais de uma inscrição municipal."],
            "atualizado_em": "", "serie": "3"}}), "utf-8")

    def test_em030_cliente_sem_nota_sai_dos_lotes_do_painel_e_vai_para_o_relatorio(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        lote = self.criar_lote(origem, modo="todas")
        vendas = emissor.carregar(lote)[1].vendas
        venda = vendas[0]
        self.assertFalse(venda.sem_nota_pendente)
        self.assertNotIn("Não emitir nota para este cliente", self.cliente.get(f"/lote/{lote}").data.decode())
        self.rejeitar_por_em030(lote, venda.pedido)
        self.assertTrue(emissor.carregar(lote)[1].venda(venda.pedido).sem_nota_pendente)
        self.assertIn("Não emitir nota para este cliente (sem inscrição municipal)",
                      self.cliente.get(f"/lote/{lote}").data.decode())
        self.assertEqual(emissor.painel_remessas()["atencao"][0]["rejeitadas"], 1)
        # Só pedido rejeitado por EM030 pode ser marcado.
        token = self.csrf(f"/lote/{lote}")
        self.cliente.post(f"/lote/{lote}/sem-nota", data={"pedido": vendas[1].pedido, "csrf_token": token})
        self.assertFalse((emissor.config.DADOS_DIR / emissor.ARQUIVO_SEM_NOTA).exists())
        # Marca o cliente: as vendas dele saem do lote e da emissão, mas aparecem em "Sem nota".
        doc = venda.cliente.documento
        do_cliente = [v.pedido for v in vendas if v.cliente.documento == doc]
        self.cliente.post(f"/lote/{lote}/sem-nota", data={"pedido": venda.pedido, "csrf_token": token})
        remessa = emissor.carregar(lote)[1]
        self.assertEqual({v.pedido for v in remessa.sem_nota}, set(do_cliente))
        self.assertTrue(all(v.pedido not in do_cliente for v in remessa.vendas + remessa.prontas))
        pagina = self.cliente.get(f"/lote/{lote}").data.decode()
        self.assertIn("Sem nota — decisão da empresa", pagina)
        self.assertNotIn("Não emitir nota para este cliente (sem inscrição municipal)", pagina)
        self.assertIn("Sem nota — decisão da empresa", self.cliente.get(f"/lote/{origem}").data.decode())
        # Painel: não conta como erro nem como pendência.
        linha = emissor.painel_remessas()["atencao"][0]
        self.assertEqual((linha["rejeitadas"], linha["sem_nota"], linha["vendas"]),
                         (0, len(do_cliente), len(vendas) - len(do_cliente)))
        self.assertEqual(emissor.painel_remessas()["resumo"]["sem_nota"], len(do_cliente))
        inicio = self.cliente.get("/").data.decode()
        self.assertIn("Sem nota — decisão da empresa", inicio)
        self.assertIn("Total R$ de notas emitidas", inicio)
        # Relatório em CSV.
        texto = self.cliente.get("/sem-nota.csv").data.decode("utf-8-sig")
        self.assertTrue(texto.startswith("pedido;boleto;cliente;cnpj_cpf"))
        self.assertEqual(len(texto.strip().splitlines()) - 1, len(do_cliente))
        self.assertIn(venda.pedido, texto)
        self.assertIn(doc, texto)
        # Desfazer devolve as vendas.
        self.cliente.post("/sem-nota/desfazer", data={"documento": doc, "csrf_token": token})
        self.assertEqual(emissor.carregar(lote)[1].sem_nota, [])
        self.assertEqual(emissor.painel_remessas()["resumo"]["sem_nota"], 0)

    def test_em030_cliente_sem_nota_fica_de_fora_de_lote_de_producao_novo(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        vendas = emissor.carregar(origem)[1].vendas
        doc = vendas[0].cliente.documento
        do_cliente = {v.pedido for v in vendas if v.cliente.documento == doc}
        emissor.gravar_json(emissor.config.DADOS_DIR / emissor.ARQUIVO_SEM_NOTA, {doc: {
            "cliente": "x", "pedido": vendas[0].pedido, "usuario": "t", "em": "2026-11-09T10:00:00-03:00"}})
        lote = self.criar_lote(origem, modo="todas")
        em_lote = {v.pedido for v in emissor.carregar(lote)[1].vendas}
        self.assertFalse(em_lote & do_cliente)
        self.assertEqual(len(em_lote), len(vendas) - len(do_cliente))


    def test_em030_com_nota_ja_autorizada_nao_tira_a_venda(self):
        import json
        self.entrar_como_admin()
        origem = self.importar_remessa()
        venda = emissor.carregar(origem)[1].vendas[0]
        (emissor.LOTES_DIR / origem / "envios.json").write_text(json.dumps({venda.pedido: {
            "pedido": venda.pedido, "numero_dps": 1, "situacao": "autorizada", "numero_nfse": "9",
            "mensagens": [], "atualizado_em": "", "serie": "3"}}), "utf-8")
        emissor.gravar_json(emissor.config.DADOS_DIR / emissor.ARQUIVO_SEM_NOTA, {venda.cliente.documento: {
            "cliente": "x", "pedido": venda.pedido, "usuario": "t", "em": "2026-11-09T10:00:00-03:00"}})
        remessa = emissor.carregar(origem)[1]
        self.assertIsNotNone(remessa.venda(venda.pedido))  # nota emitida é registro fiscal: segue normal

    def test_filtro_de_datas_e_total_em_reais_das_notas_emitidas(self):
        import json
        self.entrar_como_admin()
        origem = self.importar_remessa()
        lote = self.criar_lote(origem, modo="todas")
        remessa = emissor.carregar(lote)[1]
        envios = {v.pedido: {"pedido": v.pedido, "numero_dps": 100 + i, "situacao": "autorizada",
                             "numero_nfse": str(500 + i), "mensagens": [], "atualizado_em": "", "serie": "3"}
                  for i, v in enumerate(remessa.vendas[:3])}
        (emissor.LOTES_DIR / lote / "envios.json").write_text(json.dumps(envios), "utf-8")
        esperado = sum((v.valor_total for v in remessa.vendas[:3]), emissor.Decimal("0.00"))
        painel = emissor.painel_remessas()
        self.assertEqual((painel["resumo"]["emitidas"], painel["resumo"]["valor_emitido"]), (3, esperado))
        self.assertIn(f"R$ {emissor.valor_br(esperado)}", self.cliente.get("/").data.decode())
        dia = origem[:4] + "-" + origem[4:6] + "-" + origem[6:8]
        self.assertEqual(emissor.painel_remessas(dia, dia)["resumo"]["emitidas"], 3)
        self.assertEqual(emissor.painel_remessas("2000-01-01", "2000-12-31")["resumo"],
                         {k: (emissor.Decimal("0.00") if k == "valor_emitido" else 0)
                          for k in emissor.painel_remessas()["resumo"]})
        fora = self.cliente.get("/?de=2000-01-01&ate=2000-12-31").data.decode()
        self.assertIn("Mostrando só as remessas importadas", fora)
        self.assertIn("Nenhuma pendência", fora)
        dentro = self.cliente.get(f"/?de={dia}&ate={dia}").data.decode()
        self.assertIn("Precisam de atenção (1)", dentro)
        # datas inválidas são ignoradas; invertidas são trocadas
        self.assertIn("Precisam de atenção (1)", self.cliente.get("/?de=abc&ate=").data.decode())
        self.assertIn("Precisam de atenção (1)", self.cliente.get(f"/?de=2999-01-01&ate={dia}").data.decode())

    def test_cep_que_o_iss_net_ja_recusou_avisa_e_oferece_os_aceitos(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        _, remessa, _ = emissor.carregar(origem)
        ruim, outra = remessa.vendas[0], remessa.vendas[1]
        dia = config.DADOS_DIR / "comunicacao" / "2026-10-30"
        dia.mkdir(parents=True)

        def registrar(nome, venda, cep, resposta):
            (dia / f"{nome}_GerarNfse_envio.xml").write_text(
                f"<toma><end><endNac><cMun>{venda.cliente.cod_municipio}</cMun><CEP>{cep}</CEP></endNac></end></toma>", "utf-8")
            (dia / f"{nome}_GerarNfse_resposta.xml").write_text(resposta, "utf-8")

        registrar("100000_1", ruim, ruim.cliente.cep, "<ListaMensagemRetorno><Codigo>E0240</Codigo></ListaMensagemRetorno>")
        registrar("100001_2", ruim, "99999000", "<nNFSe>1</nNFSe>")  # CEP já aceito no mesmo município
        registrar("100002_3", outra, "11111000", "<nNFSe>2</nNFSe>")  # aceito, mas em outro município
        pagina = self.cliente.get(f"/lote/{origem}").data.decode()
        venda = emissor.carregar(origem)[1].venda(ruim.pedido)
        self.assertTrue(venda.cep_recusado and venda.cep_corrigivel)
        self.assertTrue(any(a.startswith("CEP recusado pelo ISS Net") for a in venda.avisos))
        self.assertEqual([s["cep"] for s in venda.sugestoes_cep if s["origem"] == "iss_aceito"], ["99999000"])
        self.assertIn("já recusou (E0240)", pagina)
        self.assertIn("O ISS Net já aceitou este CEP", pagina)
        # CEP aceito pelo ISS Net pode ser aplicado pela tela (é uma das sugestões).
        self.cliente.post(f"/lote/{origem}/aplicar-cep", data={"pedido": ruim.pedido, "cep": "99999000",
                                                              "csrf_token": self.csrf(f"/lote/{origem}")})
        venda = emissor.carregar(origem)[1].venda(ruim.pedido)
        self.assertEqual(venda.cliente.cep, "99999000")
        self.assertFalse(venda.cep_recusado)

    def test_municipio_do_cadastro_errado_pode_ser_corrigido_com_confirmacao(self):
        from nfse.cep import ResultadoCep
        self.entrar_como_admin()
        origem = self.importar_remessa()
        _, remessa, _ = emissor.carregar(origem)
        ruim = remessa.vendas[0]
        cep_ruim, ibge_antigo = ruim.cliente.cep, ruim.cliente.cod_municipio
        alternativa = {"ibge": "4307906", "cidade": "Farroupilha", "uf": "RS", "cep": cep_ruim,
                       "logradouro": "Rua X", "bairro": "Y"}

        def verificar(cep, ibge, uf, logradouro, bairro="", documento="", buscar_ruas=True):
            if cep == cep_ruim:
                return ResultadoCep("divergente", "Farroupilha", "4307906", [], alternativa)
            return ResultadoCep("ok")

        with mock.patch.object(emissor.servico_cep, "verificar", side_effect=verificar), \
                mock.patch.object(emissor.servico_cep, "nome_do_municipio", return_value="Caxias do Sul"):
            self.cliente.post(f"/lote/{origem}/verificar-ceps", data={"csrf_token": self.csrf(f"/lote/{origem}")})
            venda = emissor.carregar(origem)[1].venda(ruim.pedido)
            self.assertFalse(venda.pronta)
            self.assertEqual(venda.alternativa_municipio["cidade"], "Farroupilha")
            # Pedido sem alternativa indicada pela verificação não pode ter o município trocado.
            outro = emissor.carregar(origem)[1].vendas[1].pedido
            self.cliente.post(f"/lote/{origem}/aplicar-municipio", data={"pedido": outro,
                                                                        "csrf_token": self.csrf(f"/lote/{origem}")})
            self.assertEqual(emissor.carregar(origem)[1].venda(outro).cliente.cod_municipio, remessa.venda(outro).cliente.cod_municipio)
            self.cliente.post(f"/lote/{origem}/aplicar-municipio", data={"pedido": ruim.pedido,
                                                                        "csrf_token": self.csrf(f"/lote/{origem}")})
        venda = emissor.carregar(origem)[1].venda(ruim.pedido)
        self.assertTrue(venda.pronta)
        self.assertEqual((venda.cliente.cod_municipio, venda.cliente.uf), ("4307906", "RS"))
        self.assertTrue(any("Município do tomador corrigido para Farroupilha/RS" in a for a in venda.avisos))
        self.assertNotEqual(ibge_antigo, "4307906")
        self.assertIn("corrigiu o município", autenticacao.ARQUIVO_AUDITORIA.read_text("utf-8"))
        lote = self.criar_lote(origem, modo="todas")  # a correção acompanha a venda ao lote de produção
        self.assertEqual(emissor.carregar(lote)[1].venda(ruim.pedido).cliente.cod_municipio, "4307906")

    def test_emitir_em_producao_exige_serie_autorizada_e_numeros_reservados(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        lote = self.criar_lote(origem, modo="todas")
        with mock.patch.object(emissor, "pronto_para_emitir") as pronto:
            pagina = self.cliente.post(f"/lote/{lote}/emitir", data={"confirmo": "sim", "csrf_token": self.csrf(f"/lote/{lote}")},
                                       follow_redirects=True).data.decode()
            pronto.assert_not_called()  # nem chega a falar com o ISS Net
        self.assertIn("Antes de emitir, clique em", pagina)
        # Com o certificado carregado, a tela esconde o botão Emitir e mostra o passo obrigatório.
        emissor.sessao_certificado["certificado"] = object()
        try:
            pagina_lote = self.cliente.get(f"/lote/{lote}").data.decode()
        finally:
            emissor.sessao_certificado["certificado"] = None
        self.assertIn("Passo obrigatório antes de emitir", pagina_lote)
        self.assertNotIn("Emitir 10 notas em PRODUÇÃO", pagina_lote)
        # Com a série definida mas sem números reservados para todas as vendas, continua bloqueado.
        (emissor.LOTES_DIR / lote / "serie.txt").write_text("3", "utf-8")
        pagina = self.cliente.post(f"/lote/{lote}/emitir", data={"confirmo": "sim", "csrf_token": self.csrf(f"/lote/{lote}")},
                                   follow_redirects=True).data.decode()
        self.assertIn("ainda sem número", pagina)

    def test_criar_lote_verifica_ceps_e_deixa_de_fora_vendas_incompativeis(self):
        from nfse.cep import ResultadoCep
        self.entrar_como_admin()
        origem = self.importar_remessa()
        _, remessa, _ = emissor.carregar(origem)
        ruim = remessa.vendas[0]

        def verificar(cep, ibge, uf, logradouro, bairro="", documento="", buscar_ruas=True):
            if cep == ruim.cliente.cep:
                return ResultadoCep("divergente", "Outra Cidade", "9999999")
            return ResultadoCep("ok")

        with mock.patch.object(emissor.servico_cep, "verificar", side_effect=verificar):
            lote = self.criar_lote(origem, modo="proximas", quantidade="3")
        _, r_lote, amb = emissor.carregar(lote)
        pedidos = [v.pedido for v in r_lote.vendas]
        self.assertEqual((len(pedidos), amb), (3, "producao"))
        self.assertNotIn(ruim.pedido, pedidos)  # ficou de fora e outra venda entrou no lugar
        self.assertIn(ruim.pedido, emissor.pedidos_ja_em_producao() | {v.pedido for v in emissor.carregar(origem)[1].vendas})
        self.assertNotIn(ruim.pedido, emissor.pedidos_ja_em_producao())  # continua na remessa, para ser corrigida
        self.assertIn("bloqueadas por CEP", autenticacao.ARQUIVO_AUDITORIA.read_text("utf-8"))

    def test_criar_lote_sem_tempo_para_verificar_nao_cria_nada(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        with mock.patch.object(emissor, "ORCAMENTO_VERIFICACAO_CEP", -1):
            token = self.csrf(f"/lote/{origem}")
            resposta = self.cliente.post(f"/lote/{origem}/criar-lote-producao", data={"modo": "todas", "csrf_token": token},
                                         follow_redirects=True)
        self.assertIn("ainda faltam", resposta.data.decode())
        self.assertEqual([p for p in emissor.lotes_existentes() if p.name != origem], [])

    def test_historico_de_retorno_marca_notas_enviadas(self):
        self.entrar_como_admin()
        origem = self.importar_remessa()
        pasta = emissor.LOTES_DIR / self.criar_lote(origem, modo="proximas", quantidade="2")
        with emissor.app.test_request_context():
            emissor.registrar_retorno(pasta, "00000nota_x.txt", ["772325", "772326"], "novas")
        self.assertEqual(emissor.ja_retornadas(pasta), {"772325", "772326"})
        self.assertEqual(emissor.resumo_lote(pasta)["retornadas"], 2)


if __name__ == "__main__":
    unittest.main()
