# -*- coding: utf-8 -*-
"""
Remplaçant minimal de PyYAML, installé seulement si PyYAML ne peut pas
s'installer sur ce PC (pas de version précompilée, pas de compilateur).

Les librairies de l'IA (huggingface_hub, timm) importent "yaml" mais Tri Photos
n'utilise jamais les fonctions qui en ont besoin. Ce module permet l'import ;
toute utilisation réelle lève une erreur claire.
"""


class YAMLError(Exception):
    pass


def _absent(*args, **kwargs):
    raise YAMLError("PyYAML n'est pas installé sur ce PC (remplaçant minimal de Tri Photos).")


safe_load = load = full_load = unsafe_load = _absent
safe_dump = dump = safe_load_all = load_all = dump_all = _absent


class _Factice:
    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def add_constructor(cls, *args, **kwargs):
        pass

    @classmethod
    def add_representer(cls, *args, **kwargs):
        pass

    add_multi_representer = add_implicit_resolver = add_multi_constructor = add_constructor


def __getattr__(nom):
    if nom.startswith("__"):
        raise AttributeError(nom)
    return _Factice
