# IBKR secret files

Put the IB Gateway credentials here, one value per file, on the server only. Git ignores everything in this folder except this README and the `.gitignore`.

| File | Holds | Used by |
|------|-------|---------|
| `paper_username.txt` | The paper account's username | `ib-gateway-paper` |
| `paper_password.txt` | The paper account's password | `ib-gateway-paper` |
| `live_username.txt` | The dedicated live API username | `ib-gateway-live` |
| `live_password.txt` | Its password | `ib-gateway-live` |

How to create them safely: [../README.md](../README.md#secret-files).
