"""Publication programmée sur Instagram (lancé par GitHub Actions, toutes les 15 min).

Ce fichier est recopié ici par le Pilote Servart (dossier servart-reseaux,
outils/pilote/depot/) : le modifier là-bas, pas ici.

- programme/<nom>.json : publications confirmées par le porteur dans le Pilote.
  Quand l'heure est passée : publiées sur Instagram par l'API officielle de
  Meta, puis rangées dans publie/<nom>.json (identifiant + lien).
- 3 échecs, ou plus de 12 h de retard : rangées dans erreurs/<nom>.json, sans
  publier (le porteur décide).
- Ensuite (reprises.py) : la même publication est reprise sur Threads et sur
  la page Facebook, si leurs secrets sont là. Un échec là n'affecte jamais
  Instagram : retenté au passage suivant, 3 essais.
- --renouveler (le lundi) : prolonge les jetons Instagram et Threads de 60
  jours et les remet dans les secrets du dépôt.

Secrets du dépôt : IG_JETON, IG_USER_ID, NTFY_CANAL (notifications),
GH_SECRETS (jeton GitHub qui peut écrire les secrets, pour --renouveler) ;
facultatifs : TH_JETON, TH_USER_ID (Threads), FB_PAGE_ID, FB_PAGE_JETON (Facebook).
"""
import datetime, glob, json, os, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

try:
    import reprises
except Exception:  # un souci dans reprises.py ne doit jamais empêcher Instagram
    reprises = None

ICI = os.path.dirname(os.path.abspath(__file__))
API = "https://graph.instagram.com/v23.0/"
JETON = os.environ.get("IG_JETON", "").strip()
USER = os.environ.get("IG_USER_ID", "").strip()
RETARD_MAX = datetime.timedelta(hours=12)
ESSAIS_MAX = 3


def notifier(titre, message):
    canal = os.environ.get("NTFY_CANAL", "").strip()
    if not canal:
        return
    try:
        corps = json.dumps({"topic": canal, "title": titre, "message": message, "tags": ["art"]}).encode()
        urllib.request.urlopen(urllib.request.Request("https://ntfy.sh", data=corps,
                                                      headers={"Content-Type": "application/json"}), timeout=15)
    except Exception:
        pass


def git(*args):
    return subprocess.run(["git", *args], cwd=ICI, capture_output=True, text=True)


def enregistrer(message):
    """Commit + push tout de suite (sinon une publication pourrait repartir au passage suivant)."""
    git("add", "-A")
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("commit", "-q", "-m", message)
    for _ in range(5):
        git("pull", "-q", "--rebase")
        if git("push", "-q").returncode == 0:
            return
        time.sleep(5)
    print("::error::push impossible après 5 essais")


def appel(chemin, params=None, post=False):
    donnees = urllib.parse.urlencode(dict(params or {}, access_token=JETON))
    requete = (urllib.request.Request(API + chemin, data=donnees.encode()) if post
               else urllib.request.Request(API + chemin + "?" + donnees))
    try:
        with urllib.request.urlopen(requete, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode("utf-8", "replace")[:400]) from None


def attendre(conteneur, essais=36):
    for _ in range(essais):
        statut = appel(conteneur, {"fields": "status_code"}).get("status_code")
        if statut == "FINISHED":
            return
        if statut in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram n'a pas pu préparer le média ({statut})")
        time.sleep(5)
    raise RuntimeError("Instagram n'a pas fini de préparer le média (3 min)")


def conteneur(url, alt, enfant, legende=None):
    params = {"image_url": url}
    if enfant:
        params["is_carousel_item"] = "true"
    if legende is not None:
        params["caption"] = legende
    if alt:
        try:
            return appel(USER + "/media", dict(params, alt_text=alt), post=True)["id"]
        except RuntimeError as e:
            if "alt_text" not in str(e):
                raise
    return appel(USER + "/media", params, post=True)["id"]


def deja_publie(p):
    """Garde-fou : la même légende est-elle déjà en ligne (passage précédent interrompu) ?"""
    debut = " ".join(p["legende"].split())[:120]
    for m in appel(USER + "/media", {"fields": "id,caption,permalink", "limit": "10"}).get("data", []):
        if " ".join((m.get("caption") or "").split())[:120] == debut:
            return m["id"], m.get("permalink", "")
    return None


def publier(p):
    if p.get("video"):  # Reel : Instagram récupère la vidéo, la prépare (jusqu'à 10 min), puis on publie
        c = appel(USER + "/media", {"media_type": "REELS", "video_url": p["video"], "caption": p["legende"],
                                    "share_to_feed": "true", "thumb_offset": str(p.get("couverture_ms", 0))},
                  post=True)["id"]
        attendre(c, 120)
        media = appel(USER + "/media_publish", {"creation_id": c}, post=True)["id"]
        return media, appel(media, {"fields": "permalink"}).get("permalink", "")
    images, alts = p["images"], p.get("textes_alternatifs") or []
    alt = lambda i: alts[i] if i < len(alts) else ""
    if len(images) == 1:
        c = conteneur(images[0], alt(0), False, p["legende"])
    else:
        enfants = [conteneur(u, alt(i), True) for i, u in enumerate(images)]
        for e in enfants:
            attendre(e)
        c = appel(USER + "/media", {"media_type": "CAROUSEL", "children": ",".join(enfants),
                                    "caption": p["legende"]}, post=True)["id"]
    attendre(c)
    media = appel(USER + "/media_publish", {"creation_id": c}, post=True)["id"]
    return media, appel(media, {"fields": "permalink"}).get("permalink", "")


def ranger(dossier, nom, donnees, ancien):
    os.makedirs(os.path.join(ICI, dossier), exist_ok=True)
    with open(os.path.join(ICI, dossier, nom + ".json"), "w", encoding="utf-8") as f:
        json.dump(donnees, f, ensure_ascii=False, indent=1)
    os.remove(ancien)


def passage():
    maintenant = datetime.datetime.now(datetime.timezone.utc)
    for f in sorted(glob.glob(os.path.join(ICI, "programme", "*.json"))):
        with open(f, encoding="utf-8") as fi:
            p = json.load(fi)
        nom, titre = p["nom"], p.get("titre", p["nom"])
        quand = datetime.datetime.fromisoformat(p["publier_le"])
        if quand > maintenant:
            continue
        if maintenant - quand > RETARD_MAX:
            p["erreur"] = f"Pas publiée : plus de 12 h de retard (prévue le {p['publier_le']})."
            ranger("erreurs", nom, p, f)
            enregistrer(f"En erreur : {titre}")
            notifier("Publication non faite", f"« {titre} » : trop en retard, rien n'a été publié.")
            continue
        try:
            if not JETON or not USER:
                raise RuntimeError("secrets IG_JETON ou IG_USER_ID absents du dépôt")
            media, lien = deja_publie(p) or publier(p)
        except Exception as e:
            p["essais"] = p.get("essais", 0) + 1
            p["erreur"] = str(e)
            if p["essais"] >= ESSAIS_MAX:
                ranger("erreurs", nom, p, f)
                notifier("Publication en échec", f"« {titre} » : {str(e)[:200]}")
            else:
                with open(f, "w", encoding="utf-8") as fi:
                    json.dump(p, fi, ensure_ascii=False, indent=1)
            enregistrer(f"Échec ({p['essais']}/{ESSAIS_MAX}) : {titre}")
            continue
        ranger("publie", nom, {"nom": nom, "titre": titre, "prevue_le": p["publier_le"],
                               "publiee_le": maintenant.isoformat(timespec="minutes"),
                               "media_id": media, "lien": lien,
                               "reprise": {"legende": p["legende"], "images": p.get("images"),
                                           "video": p.get("video")}}, f)
        enregistrer(f"Publié : {titre}")
        notifier("Publié sur Instagram", f"« {titre} » est en ligne. {lien}")
    rattraper()


def rattraper():
    """Reprend sur Threads et Facebook les publications Instagram des dernières heures (jamais bloquant)."""
    if not reprises or not reprises.actifs():
        return
    maintenant = datetime.datetime.now(datetime.timezone.utc)
    for f in sorted(glob.glob(os.path.join(ICI, "publie", "*.json"))):
        try:
            with open(f, encoding="utf-8") as fi:
                rec = json.load(fi)
            if not reprises.a_faire(rec, maintenant):
                continue
            messages = reprises.reprendre(rec, maintenant)
            with open(f, "w", encoding="utf-8") as fo:
                json.dump(rec, fo, ensure_ascii=False, indent=1)
            titre = rec.get("titre", rec.get("nom", ""))
            enregistrer(f"Reprise Threads / Facebook : {titre}")
            if messages:
                notifier("Repris sur Threads / Facebook", f"« {titre} »\n" + "\n".join(messages))
        except Exception as e:
            print("::warning::reprise impossible :", str(e)[:200])


def renouveler():
    try:
        if not JETON:
            raise RuntimeError("secret IG_JETON absent")
        url = ("https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token="
               + JETON)
        with urllib.request.urlopen(url, timeout=30) as r:
            nouveau = json.load(r)["access_token"]
        print("::add-mask::" + nouveau)
        env = dict(os.environ, GH_TOKEN=os.environ.get("GH_SECRETS", ""))
        r = subprocess.run(["gh", "secret", "set", "IG_JETON", "--repo", os.environ["GITHUB_REPOSITORY"]],
                           input=nouveau, text=True, capture_output=True, env=env)
        if r.returncode:
            raise RuntimeError("secret non mis à jour (GH_SECRETS absent ou sans droit « Secrets »)")
    except Exception as e:
        notifier("Jeton Instagram à vérifier (GitHub)", f"Renouvellement impossible : {str(e)[:200]}")
        raise SystemExit(1)
    renouveler_threads()
    # garde le dépôt actif : GitHub coupe les tâches planifiées après 60 jours sans commit
    with open(os.path.join(ICI, "etat.txt"), "w", encoding="utf-8") as f:
        f.write(f"Jeton Instagram renouvelé le {datetime.date.today():%d/%m/%Y}.\n")
    enregistrer("Jeton Instagram renouvelé")


def renouveler_threads():
    """Jeton Threads (60 jours) : prolongé chaque lundi. Un échec prévient le porteur mais n'arrête rien."""
    if not reprises or "threads" not in reprises.actifs():
        return
    try:
        nouveau = reprises.renouveler_threads()
        print("::add-mask::" + nouveau)
        env = dict(os.environ, GH_TOKEN=os.environ.get("GH_SECRETS", ""))
        r = subprocess.run(["gh", "secret", "set", "TH_JETON", "--repo", os.environ["GITHUB_REPOSITORY"]],
                           input=nouveau, text=True, capture_output=True, env=env)
        if r.returncode:
            raise RuntimeError("secret non mis à jour (GH_SECRETS absent ou sans droit « Secrets »)")
    except Exception as e:
        notifier("Jeton Threads à vérifier (GitHub)", f"Renouvellement impossible : {str(e)[:200]}")


def tester():
    """Lancement à la main (« Run workflow ») : vérifie les 4 secrets sans rien publier."""
    bilan = []
    try:
        nom = appel(USER, {"fields": "username"}).get("username", "?")
        bilan.append(f"Instagram OK (@{nom})")
    except Exception as e:
        bilan.append(f"Instagram EN ÉCHEC : IG_JETON ou IG_USER_ID ({str(e)[:150]})")
    if reprises:
        if os.environ.get("TH_JETON", "").strip():
            try:
                nom = reprises.appel(reprises.THREADS, "me", reprises.secret("TH_JETON"), {"fields": "username"}).get("username", "?")
                bilan.append(f"Threads OK (@{nom})")
            except Exception as e:
                bilan.append(f"Threads EN ÉCHEC : TH_JETON ({str(e)[:150]})")
        else:
            bilan.append("Threads non branché")
        if os.environ.get("FB_PAGE_JETON", "").strip():
            try:
                nom = reprises.appel(reprises.FACEBOOK, reprises.secret("FB_PAGE_ID"), reprises.secret("FB_PAGE_JETON"),
                                     {"fields": "name"}).get("name", "?")
                bilan.append(f"Facebook OK ({nom})")
            except Exception as e:
                bilan.append(f"Facebook EN ÉCHEC : FB_PAGE_ID ou FB_PAGE_JETON ({str(e)[:150]})")
        else:
            bilan.append("Facebook non branché")
    env = dict(os.environ, GH_TOKEN=os.environ.get("GH_SECRETS", ""))
    r = subprocess.run(["gh", "secret", "list", "--repo", os.environ.get("GITHUB_REPOSITORY", "")],
                       capture_output=True, text=True, env=env)
    bilan.append("GH_SECRETS OK" if r.returncode == 0 else "GH_SECRETS EN ÉCHEC (jeton GitHub ou droit « Secrets »)")
    message = " · ".join(bilan)
    print(message)
    notifier("Test du dépôt servart-images", message)  # arrive seulement si NTFY_CANAL est bon
    if "ÉCHEC" in message:
        raise SystemExit(1)


if __name__ == "__main__":
    if "--tester" in sys.argv:
        tester()
    else:
        renouveler() if "--renouveler" in sys.argv else passage()
