# Changelog: generación manual y por hooks

`CHANGELOG.md` sigue el formato [Keep a Changelog](https://keepachangelog.com/).

## Generación manual

```bash
# Desde la raíz del repo:
bash scripts/generate-changelog.sh v1.0.1 v1.2.0
```

Agrega entradas formateadas a `CHANGELOG.md` a partir de los mensajes de commit.
Revisa y edita el resultado antes de commitear.

El script clasifica los commits por prefijo:

| Sección | Prefijos reconocidos |
|---|---|
| Added | `feat`, `add`, `feature`, `implement`, `create` |
| Changed | `change`, `update`, `refactor`, `modify`, `optimize`, `improve` |
| Fixed | `fix`, `bug`, `correct`, `resolve` |
| Removed | `remove`, `delete`, `drop` |

Cualquier otro prefijo (`docs`, `chore`, `wip`, …) cae en "Unclassified".

## Generación automática (git hooks)

Dos hooks en `.githooks/`:

| Hook | Disparador | Comportamiento |
|---|---|---|
| `post-commit` | `git commit` local | Actualiza `CHANGELOG.md` en ramas de feature; solo preview en `master`/`dev` |
| `post-merge` | `git pull` o `git merge` | Actualiza `CHANGELOG.md` localmente (no hace amend — las ramas pueden estar protegidas) |

## Ramas protegidas

`master` y `dev` aceptan cambios **solo por PR**. Por eso los hooks:

- se ejecutan localmente al hacer `git pull` del PR mergeado (`post-merge`);
- actualizan `CHANGELOG.md` en el working tree **sin commitear**;
- en rama protegida, te indican que abras un PR nuevo con el cambio de changelog;
- en rama de feature, permiten `git add && git commit` directo.

## Instalación de los hooks

```bash
git config core.hooksPath .githooks
chmod +x .githooks/post-commit .githooks/post-merge
```

## Flujo típico tras mergear un PR

```bash
git checkout dev && git pull
# post-merge dispara → CHANGELOG.md actualizado localmente

git checkout -b update-changelog
git add CHANGELOG.md && git commit -m "update: CHANGELOG tras PR #NN"
git push origin update-changelog
# abrir PR → dev
```

Usa un prefijo de la tabla (`update:`) para que el propio cambio quede clasificado
en el próximo release.
