#!/usr/bin/env python3
"""Apply fail-closed local privacy fixes after Corten's source prep."""

from __future__ import annotations

import argparse
from pathlib import Path


REPLACEMENTS: dict[str, list[tuple[str, str]]] = {
    "src/auth.rs": [
        (
            '                println!("Body {}", encode_hex(&step1));\n\n                self.account.lock().await.circle(&CircleSendMessage {',
            '                self.account.lock().await.circle(&CircleSendMessage {',
        ),
    ],
    "src/util.rs": [
        (
            '                    println!("paniced with {:?}", string);\n                    PushError::ResourcePanic(string)',
            '                    PushError::ResourcePanic(string)',
        ),
    ],
    "src/sharedstreams.rs": [
        (
            '        while let Ok(Some(res)) = tokio::time::timeout(if dirty_albums.is_empty() { Duration::MAX } else { Duration::from_secs(30) }, reciever.recv()).await {\n            println!("what {:?}", res);\n            match res {',
            '        while let Ok(Some(res)) = tokio::time::timeout(if dirty_albums.is_empty() { Duration::MAX } else { Duration::from_secs(30) }, reciever.recv()).await {\n            match res {',
        ),
    ],
    "src/icloud/mmcs.rs": [
        (
            '            let data = cipher.decrypt::<&[&[u8]], &&[u8]>(&[&ford[1..17], &ford[..1]], &ford[17..]).unwrap();\n            println!("{}", encode_hex(&data));\n\n            let chunks = FordChunk::decode(Cursor::new(&data))?;',
            '            let data = cipher.decrypt::<&[&[u8]], &&[u8]>(&[&ford[1..17], &ford[..1]], &ford[17..]).unwrap();\n            let chunks = FordChunk::decode(Cursor::new(&data))?;',
        ),
        (
            '            total_hasher.update(&ford);\n            println!("{}", encode_hex(&total_hasher.finish()))',
            '            total_hasher.update(&ford);\n            let _ = total_hasher.finish();',
        ),
        (
            '    let decrypt_final_result = decrypt_cipher.finalize(&mut decrypted_bytes[total_count..]);\n    println!("Result {:?} data {} hash {} iv {} key {} request {}", decrypt_final_result, encode_hex(&decrypted_bytes), encode_hex(&output.hasher.finish()), encode_hex(&output.iv), encode_hex(&config.decryption_key), encode_hex(request.data()));\n    total_count += decrypt_final_result.unwrap();',
            '    let decrypt_final_result = decrypt_cipher.finalize(&mut decrypted_bytes[total_count..]);\n    let Ok(final_count) = decrypt_final_result else {\n        return Err(MMCSError::Error((400, "Corrupted MMCS payload".into())))\n    };\n    total_count += final_count;',
        ),
    ],
    "src/icloud/keychain.rs": [
        (
            '                .notify(|err: &PushError, _dur: Duration| {\n                    println!("erasing keys {:?}", err);\n            }).await?;',
            '                .notify(|_err: &PushError, _dur: Duration| {\n            }).await?;',
        ),
        (
            '            .notify(|err: &PushError, _dur: Duration| {\n                println!("resetting clique {:?}", err);\n            }).await?;',
            '            .notify(|_err: &PushError, _dur: Duration| {\n            }).await?;',
        ),
        (
            '        if let Err(err) = self.hierarchy.remove_deletable_journal_entries().await {\n            println!("Failed to remove deletable journal entries: {:?}", err);\n        }\n        if let Err(err) = self.hierarchy.create_pending_delete_journal_entry().await {\n            println!("Failed to create pending delete journal entry: {:?}", err);\n        }\n        if let Err(err) = self.hierarchy.delete_existing_method_plist().await {\n            println!("Failed to delete existing method plist: {:?}", err);\n        }\n        if let Err(err) = self.hierarchy.remove_move_request_from_stash_and_creatd_pending_delete_entry().await {\n            println!("Failed to remove move request from stash and create pending delete journal entry: {:?}", err);\n        }\n        if let Err(err) = self.hierarchy.update_current_item_pointers().await {\n            println!("Failed to update current item pointers: {:?}", err);\n        }\n        if let Err(err) = self.hierarchy.upload_new_top_level_keys_and_share().await {\n            println!("Failed to upload new tlks and share: {:?}", err);\n        }',
            '        if let Err(_err) = self.hierarchy.remove_deletable_journal_entries().await {\n            warn!("Failed to remove deletable keychain journal entries");\n        }\n        if let Err(_err) = self.hierarchy.create_pending_delete_journal_entry().await {\n            warn!("Failed to create pending-delete keychain journal entry");\n        }\n        if let Err(_err) = self.hierarchy.delete_existing_method_plist().await {\n            warn!("Failed to delete existing keychain method plist");\n        }\n        if let Err(_err) = self.hierarchy.remove_move_request_from_stash_and_creatd_pending_delete_entry().await {\n            warn!("Failed to update keychain move-request stash");\n        }\n        if let Err(_err) = self.hierarchy.update_current_item_pointers().await {\n            warn!("Failed to update keychain current-item pointers");\n        }\n        if let Err(_err) = self.hierarchy.upload_new_top_level_keys_and_share().await {\n            warn!("Failed to upload keychain top-level keys and shares");\n        }',
        ),
    ],
    "src/ids/user.rs": [
        (
            '    if std::env::var("RUSTPUSH_LOG_REGISTER_BODY").is_ok() { info!("REGISTER body XML: {}", plist_to_string(&body).unwrap_or_default()); } let mut request = SignedRequest::new("id-register", Method::POST)',
            '    let mut request = SignedRequest::new("id-register", Method::POST)',
        ),
    ],
}

FORBIDDEN_RUNTIME_SNIPPETS = (
    'println!("Body {}", encode_hex(&step1))',
    'println!("{}", encode_hex(&data))',
    'println!("{}", encode_hex(&total_hasher.finish()))',
    'println!("erasing keys {:?}", err)',
    'println!("resetting clique {:?}", err)',
    'println!("Result {:?} data {} hash {} iv {} key {} request {}"',
    'RUSTPUSH_LOG_REGISTER_BODY',
)


def replace_once(path: Path, old: str, new: str) -> str:
    text = path.read_text()
    count = text.count(old)
    if count == 1:
        path.write_text(text.replace(old, new, 1))
        return "applied"
    if count == 0 and new in text:
        return "already-applied"
    if count == 0:
        return "not-present"
    raise RuntimeError(f"{path}: expected at most one source match, found {count}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("rustpush_root", type=Path)
    args = parser.parse_args()
    root = args.rustpush_root.resolve()
    if not (root / "Cargo.toml").is_file():
        raise SystemExit(f"not a RustPush checkout: {root}")

    for relative, replacements in REPLACEMENTS.items():
        path = root / relative
        for old, new in replacements:
            print(f"{relative}: {replace_once(path, old, new)}")

    production_text = "\n".join(
        p.read_text(errors="replace")
        for p in (root / "src").rglob("*.rs")
        if p.name != "test.rs"
    )
    found = [item for item in FORBIDDEN_RUNTIME_SNIPPETS if item in production_text]
    if found:
        raise RuntimeError(f"secret-bearing runtime diagnostics remain: {found}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
