"""Formato de números a la convención colombiana (DEC-083/084).

Existe porque el idiom que reemplaza —`f"{n:,}".replace(",", ".")`— es un
error silencioso en cuanto la cadena lleva prosa: la concatenación implícita
de literales ocurre antes que la llamada al método, así que el `.replace()`
se come las comas de la frase. Ya se coló una vez.
"""

import pytest

from comun import formato_miles

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("valor", "decimales", "esperado"),
    [
        (1234, 0, "1.234"),
        (1234567, 0, "1.234.567"),
        (999, 0, "999"),
        (0, 0, "0"),
        (1234.56, 2, "1.234,56"),
        (36.2, 1, "36,2"),
        (-400, 0, "-400"),
        (-1234.5, 1, "-1.234,5"),
    ],
)
def test_formatea_a_la_convencion_colombiana(valor, decimales, esperado):
    assert formato_miles(valor, decimales) == esperado


def test_no_toca_la_prosa_que_lo_rodea():
    """El defecto que motivó el helper: `.replace(",", ".")` sobre la frase
    entera convertía «el valor del pedido, y entre todos» en «pedido. y»."""
    frase = f"En {formato_miles(1234)} pedidos, con coma, y otra."

    assert frase == "En 1.234 pedidos, con coma, y otra."


def test_el_idiom_viejo_si_rompe_la_prosa():
    """Se fija el comportamiento defectuoso para que quede claro qué se está
    evitando — y que no vuelva por copiar y pegar."""
    roto = f"En {1234:,} pedidos, con coma".replace(",", ".")

    assert roto == "En 1.234 pedidos. con coma"


def test_el_separador_intermedio_no_sobrevive():
    """La implementación usa NUL como marca de paso. Si se filtrara al
    resultado, el número saldría con un carácter invisible."""
    assert "\x00" not in formato_miles(1234567.89, 2)


def test_redondea_en_vez_de_truncar():
    assert formato_miles(1234.567, 2) == "1.234,57"
    assert formato_miles(0.5, 0) == "0"  # banker's rounding, igual que f-string
