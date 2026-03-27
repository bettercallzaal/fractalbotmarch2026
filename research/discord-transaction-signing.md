# Onchain Transaction Signing from a Discord Bot

**Research for ZAO Fractal Bot -- Automated `submitBreakout` Execution**

**Date:** 2026-03-27

---

## Context

Today the ZAO Fractal Bot generates a URL to `zao.frapps.xyz/submitBreakout` after a fractal meeting completes. A human must click that link, connect their wallet, and submit the transaction manually. The goal is to eliminate that step: after the bot posts final rankings, it should sign and submit the `submitBreakout` transaction onchain (Optimism) automatically, so nobody has to leave Discord.

The contract lives in the [Optimystics/op-fractal-sc](https://github.com/Optimystics/op-fractal-sc) repo. The call takes ranked wallet addresses and a group number as parameters, following the URL pattern:

```
/submitBreakout?groupnumber=N&vote1=0xABC...&vote2=0xDEF...
```

The bot already has all the data it needs (ranked members, wallet addresses, group number) -- the only missing piece is signing and submitting the transaction.

---

## Approach 1: Bot-Held Signing Key (Hot Wallet)

### How It Works

The bot holds an Ethereum private key in an environment variable. When a fractal completes, it uses `web3.py` to build, sign, and broadcast the `submitBreakout` transaction directly.

### Implementation

```python
import os
from web3 import Web3

# Connect to Optimism
w3 = Web3(Web3.HTTPProvider(os.environ["OPTIMISM_RPC_URL"]))
PRIVATE_KEY = os.environ["BOT_PRIVATE_KEY"]
BOT_ADDRESS = w3.eth.account.from_key(PRIVATE_KEY).address

# Contract setup (ABI would come from op-fractal-sc repo)
CONTRACT_ADDRESS = "0x..."  # ZAO Respect contract on Optimism
contract = w3.eth.contract(address=CONTRACT_ADDRESS, abi=SUBMIT_BREAKOUT_ABI)

async def submit_breakout_onchain(group_number: int, ranked_wallets: list[str]):
    """Sign and submit the breakout results onchain."""
    # Build the transaction
    tx = contract.functions.submitBreakout(
        group_number,
        ranked_wallets  # ordered list of addresses, rank 1 first
    ).build_transaction({
        'from': BOT_ADDRESS,
        'nonce': w3.eth.get_transaction_count(BOT_ADDRESS),
        'gas': 300_000,
        'maxFeePerGas': w3.to_wei('0.1', 'gwei'),        # Optimism gas is cheap
        'maxPriorityFeePerGas': w3.to_wei('0.001', 'gwei'),
        'chainId': 10,  # Optimism mainnet
    })

    # Sign and send
    signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return receipt
```

### Security Tradeoffs

**Risks:**
- If the server or env vars are compromised, the attacker gets full control of the signing key.
- The key has unrestricted signing power -- it can sign any transaction, not just `submitBreakout`.
- If the bot is hosted on a VPS or shared infra, the attack surface is larger.

**Mitigations:**
- Use a dedicated wallet with minimal ETH (just enough for gas). The bot wallet should never hold meaningful value.
- Restrict the key's permissions at the contract level if possible (e.g., the contract could allowlist the bot address as an authorized submitter).
- Use cloud secrets management (AWS Secrets Manager, GCP Secret Manager, or Railway/Fly encrypted env vars) rather than a `.env` file on disk.
- For higher security: use AWS KMS or Azure Key Vault to store the key in an HSM. The key never leaves the hardware module; signing requests are sent via API. Libraries like [aws-kms-ethereum-signing](https://aws.amazon.com/blogs/web3/make-eoa-private-keys-compatible-with-aws-kms/) enable this.

**How other DAO bots handle this:**
Most small-to-mid-size DAO bots (tip bots, airdrop bots, governance execution bots) use a hot wallet with a dedicated low-value EOA. The key is stored as an env var on the hosting platform. This is the most common pattern due to its simplicity.

### Pros
- Simplest to implement; 50-100 lines of Python.
- No external services or third-party dependencies beyond an RPC provider.
- Fully autonomous -- no human in the loop after the fractal completes.
- Fast: transaction submitted within seconds of results being finalized.
- Fits naturally into the existing bot architecture (Python, discord.py).

### Cons
- Single point of failure: key compromise = full control.
- No built-in approval flow or spending limits.
- The bot operator bears custody responsibility.

### Implementation Complexity: **Small**

---

## Approach 2: Embedded Wallets / MPC (Privy, Dynamic, Turnkey, Lit Protocol)

### How It Works

A third-party service manages the private key using cryptographic techniques (MPC, Shamir's Secret Sharing, or TEEs) so no single party ever holds the full key. The bot calls an API to request a signature.

### Key Platforms

| Platform | Technique | Server-Side Signing | Python SDK |
|----------|-----------|-------------------|------------|
| **Turnkey** | TEE (AWS Nitro Enclaves) | Yes -- designed for it | No native Python SDK; REST API available |
| **Privy** | Shamir's Secret Sharing (SSS) | Primarily frontend/user-facing | No Python SDK; REST API |
| **Dynamic** | TSS-MPC | Primarily frontend/user-facing | No Python SDK |
| **Lit Protocol** | Distributed key generation (DKG) across nodes | Yes -- Programmable Key Pairs (PKPs) | JS SDK; no Python SDK |

### Best Fit: Turnkey

Turnkey is the strongest candidate for server-side bot signing because it is explicitly designed for backend/automated transaction signing:

- Keys live in AWS Nitro Enclaves (TEEs); raw private keys are never exposed to anyone.
- Policy engine supports transaction limits, address whitelisting, and rate limiting.
- 50-100ms signing latency.
- REST API works from any language.

```python
import httpx

TURNKEY_API_URL = "https://api.turnkey.com"
TURNKEY_ORG_ID = os.environ["TURNKEY_ORG_ID"]
TURNKEY_API_KEY = os.environ["TURNKEY_API_KEY"]

async def sign_with_turnkey(unsigned_tx_bytes: bytes, wallet_id: str) -> bytes:
    """Request Turnkey to sign a transaction via their API."""
    response = await httpx.AsyncClient().post(
        f"{TURNKEY_API_URL}/v1/sign",
        headers={"Authorization": f"Bearer {TURNKEY_API_KEY}"},
        json={
            "organizationId": TURNKEY_ORG_ID,
            "walletId": wallet_id,
            "payload": unsigned_tx_bytes.hex(),
            "encoding": "hex",
        }
    )
    return bytes.fromhex(response.json()["signature"])
```

### Lit Protocol Alternative

Lit Protocol's Programmable Key Pairs (PKPs) could also work. A PKP is a distributed key managed by the Lit Network. You define a "Lit Action" (JavaScript code that runs across Lit nodes) specifying when the key is allowed to sign. The bot would trigger a Lit Action to sign the `submitBreakout` transaction. However, the SDK is JavaScript-only, which means either running a Node sidecar or using their REST API.

### Pros
- Private key never exists in a single location; significantly harder to compromise.
- Turnkey's policy engine can restrict signing to specific contract addresses and function selectors.
- Audit trail: every signing request is logged by the service.
- No key material on the bot's server at all.

### Cons
- Adds a third-party dependency and potential point of failure.
- No native Python SDKs for most platforms -- requires REST API integration or a JS sidecar.
- Monthly cost: Turnkey and Privy are paid services (Turnkey pricing is enterprise/custom).
- More complex to set up and debug than a simple hot wallet.
- Privy and Dynamic are designed for user-facing embedded wallets, not server-side bot signing -- they are not ideal for this use case.

### Implementation Complexity: **Medium**

---

## Approach 3: Gnosis Safe / Multisig

### How It Works

The bot does not sign the final transaction. Instead, it proposes a `submitBreakout` transaction to a Gnosis Safe (multisig wallet) via the Safe Transaction Service API. Human signers (ZAO admins) then approve the transaction through the Safe UI or a Discord-integrated approval flow.

### Implementation

```python
from safe_eth.eth import EthereumClient
from safe_eth.safe import Safe
from safe_eth.safe.api import TransactionServiceApi

# Connect to Optimism
eth_client = EthereumClient(os.environ["OPTIMISM_RPC_URL"])
safe = Safe(SAFE_ADDRESS, eth_client)

# Build the submitBreakout calldata
calldata = contract.functions.submitBreakout(
    group_number, ranked_wallets
).build_transaction({'gas': 0})['data']

# Build Safe multisig transaction
safe_tx = safe.build_multisig_tx(
    to=CONTRACT_ADDRESS,
    value=0,
    data=bytes.fromhex(calldata[2:]),  # strip 0x prefix
    operation=0,  # CALL
)

# Bot signs as one of N required signers
safe_tx.sign(BOT_PRIVATE_KEY)

# Propose to the Safe Transaction Service (appears in Safe UI)
tx_service = TransactionServiceApi(
    network_id=10,  # Optimism
    ethereum_client=eth_client
)
tx_service.post_transaction(safe_tx)
```

### Discord Integration Flow

1. Fractal completes -> Bot proposes transaction to Safe.
2. Bot posts in Discord: "Results submitted for approval. 2 of 3 signers needed."
3. Bot includes a direct link to the Safe UI transaction queue.
4. Signers approve via Safe{Wallet} app or website.
5. Bot monitors the Safe for execution and posts confirmation to Discord.

**Streamlining further:** The bot could act as signer 1 of N, so only one additional human approval is needed. For a 2-of-3 Safe where the bot is one signer, a single human confirmation completes the transaction.

### The Zodiac Module

The [Collab.Land Zodiac Bot Module](https://hackmd.io/@Bau/SkpqOP9Wi) is a precedent here. It extends Discord-based voting to execute transactions onchain over a Gnosis Safe. However, it is focused on general DAO governance (funding proposals, signer management), not on the specific pattern of automated Respect distribution. It may not be directly reusable but validates the architecture.

### Pros
- Human oversight: no transaction goes onchain without explicit approval.
- Gnosis Safe is battle-tested and widely trusted.
- The bot never needs full signing authority.
- Natural fit for ZAO's governance ethos.

### Cons
- Not fully automated: requires a human to approve each fractal's results.
- Adds friction and latency -- someone must open Safe UI and sign.
- Requires deploying and maintaining a Gnosis Safe on Optimism.
- `safe-eth-py` has a learning curve and the Transaction Service API requires an API key.
- If signers are unavailable, results cannot be submitted promptly.

### Implementation Complexity: **Medium**

---

## Approach 4: Account Abstraction (ERC-4337) with Session Keys

### How It Works

Instead of an EOA, the ZAO DAO uses a Smart Account (e.g., ZeroDev Kernel, Safe{Core} with 4337 module, Biconomy). The bot holds a session key -- a restricted signer that can only call specific functions (like `submitBreakout`) on specific contracts, with optional rate limits and expiry times.

### Architecture

```
ZAO Smart Account (onchain, holds permissions)
    |
    |-- Owner key (held by ZAO multisig or admin -- full control)
    |-- Session key (held by bot -- scoped permissions)
            |
            |-- Can ONLY call submitBreakout on 0x... contract
            |-- Max 10 transactions per day
            |-- Expires after 30 days (renewable)
```

### ZeroDev Session Keys

[ZeroDev](https://docs.zerodev.app/sdk/advanced/session-keys) offers the most mature session key implementation:

1. **Owner creates session key:** The ZAO admin authorizes a public key (generated by the bot) with specific policies:
   - `CallPolicy`: restrict to `submitBreakout` function on the Respect contract only.
   - `RateLimitPolicy`: max N calls per time period.
   - `TimestampPolicy`: session expires after a set date.

2. **Bot signs UserOperations:** When a fractal completes, the bot creates a UserOperation calling `submitBreakout`, signs it with the session key, and submits it to a Bundler.

3. **Bundler executes:** The Bundler packages the UserOperation and submits it onchain. The Smart Account validates the session key's permissions before executing.

### Key Limitation

ZeroDev's SDK is JavaScript/TypeScript only. For a Python bot, you would need to either:
- Run a small Node.js sidecar service that the Python bot calls via HTTP.
- Use the raw ERC-4337 UserOperation format and submit directly to a Bundler RPC (complex but possible in Python).

### Conceptual Python Integration

```python
# The bot would call a Node.js microservice or use raw 4337 UserOps

async def submit_via_session_key(group_number: int, ranked_wallets: list[str]):
    """Submit via ERC-4337 session key (calls Node.js sidecar)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://localhost:3001/submit-breakout",
            json={
                "groupNumber": group_number,
                "rankedWallets": ranked_wallets,
            }
        )
        return response.json()  # { txHash, userOpHash, ... }
```

```javascript
// sidecar/index.js -- Node.js service using ZeroDev SDK
import { createKernelAccount, createKernelAccountClient } from "@zerodev/sdk"
import { toPermissionValidator } from "@zerodev/permissions"
import { toCallPolicy } from "@zerodev/permissions/policies"

// Session key created by owner, stored as env var
const sessionKey = process.env.SESSION_KEY_DATA

const callPolicy = toCallPolicy({
  permissions: [{
    target: RESPECT_CONTRACT_ADDRESS,
    functionName: "submitBreakout",
  }]
})

// Sign and submit UserOperation
async function submitBreakout(groupNumber, rankedWallets) {
  const kernelClient = await createKernelAccountClient({ /* ... */ })
  const txHash = await kernelClient.sendUserOperation({
    callData: encodeFunctionData({
      abi: respectAbi,
      functionName: "submitBreakout",
      args: [groupNumber, rankedWallets],
    })
  })
  return txHash
}
```

### Pros
- Principle of least privilege: the bot key can ONLY call `submitBreakout`, nothing else.
- If the session key is compromised, damage is strictly bounded (attacker can only submit breakout results, not steal funds).
- Fully automated, no human in the loop.
- Session keys can be rotated or revoked without changing the Smart Account.
- Gas sponsorship possible via Paymasters (the DAO can sponsor gas so the bot wallet does not need ETH).

### Cons
- Most complex approach by far. Requires Smart Account deployment, Bundler setup, session key management.
- ZeroDev SDK is JS-only -- requires a Node.js sidecar for the Python bot.
- ERC-4337 infrastructure on Optimism adds another dependency (Bundler, Paymaster).
- Newer technology with less battle-testing than simple EOA signing.
- Overkill if the bot wallet is low-value and the contract already has access controls.

### Implementation Complexity: **Large**

---

## Approach 5: Existing Discord Bot Frameworks for Onchain Actions

### Collab.Land

[Collab.Land](https://collab.land/) is the most widely used Discord bot for token-gated communities. It can verify token holdings and manage roles. The **Zodiac Bot Module** (built with RaidGuild) extends this to execute Gnosis Safe transactions based on Discord votes. However:
- It is designed for general governance (funding proposals, signer changes), not for calling arbitrary contract functions like `submitBreakout`.
- The module is a specific integration, not a general-purpose framework.
- Customizing it for ZAO's specific flow would require forking or building on top of it.

### Guild.xyz

[Guild.xyz](https://guild.xyz/) focuses on token-gated access and role management. It does not handle transaction signing or onchain submission.

### Tip.cc and Other Tip Bots

Crypto tip bots (Tip.cc, etc.) use hot wallets internally for token transfers. They validate the pattern of a Discord bot holding a signing key, but are not extensible frameworks -- they are closed-source, purpose-built products.

### Snapshot + Reality.eth

Snapshot enables offchain voting that can trigger onchain execution via the Reality.eth oracle and a Gnosis Safe module. This is the pattern behind the Collab.Land Zodiac module. However, it requires a full governance vote for each submission, which is too heavy for weekly fractal results.

### Assessment

**There is no existing Discord bot framework that provides out-of-the-box transaction signing for arbitrary contract calls.** The existing tools are either:
- Read-only (Guild.xyz: role gating based on token holdings)
- Governance-specific (Collab.Land Zodiac: proposal-based Safe execution)
- Closed-source (tip bots)

ZAO will need to build the signing logic into the Fractal Bot directly, using one of the approaches above.

---

## Comparison Matrix

| Criteria | Hot Wallet | MPC/Embedded (Turnkey) | Gnosis Safe | ERC-4337 Session Keys |
|----------|-----------|----------------------|-------------|----------------------|
| **Implementation Complexity** | Small | Medium | Medium | Large |
| **Automation** | Full | Full | Partial (needs human) | Full |
| **Security** | Low-Medium | High | High | Very High |
| **Python Native** | Yes | REST API | Yes (safe-eth-py) | No (JS sidecar needed) |
| **Third-Party Dependencies** | RPC only | Turnkey service | Safe infra + API key | Bundler, Paymaster, ZeroDev |
| **Cost** | Gas only | Gas + Turnkey fees | Gas only | Gas + Bundler/Paymaster fees |
| **Key Compromise Impact** | Full wallet control | Bounded by policies | Requires N-of-M signers | Limited to scoped permissions |
| **Battle-Tested** | Very mature | Mature (Turnkey) | Very mature | Newer, maturing fast |
| **Fits ZAO Scale** | Perfect | Overkill | Good but adds friction | Overkill |

---

## Recommendation for ZAO

### Start with: Hot Wallet (Approach 1), with contract-level access control

**Rationale:**

1. **ZAO is a small community DAO**, not managing millions in treasury. The risk profile of a hot wallet holding minimal gas ETH on Optimism is acceptable.

2. **The bot already has everything it needs.** The `_post_submit_breakout` method in `cogs/fractal/group.py` already gathers ranked wallets and group numbers. Replacing the URL generation with a `web3.py` transaction submission is a ~100-line change.

3. **Optimism gas is extremely cheap.** A `submitBreakout` call likely costs fractions of a cent. The bot wallet needs minimal ETH.

4. **Contract-level access control is the real security layer.** If the Respect contract has (or can add) an allowlist of authorized submitters, the bot's address can be added. Even if the key is compromised, the attacker can only submit breakout results -- not drain funds, because the wallet holds negligible value.

5. **No new infrastructure.** No Bundler, no Paymaster, no Turnkey account, no Gnosis Safe. Just an RPC endpoint (Alchemy, Infura, or public Optimism RPC) and a private key.

### Implementation Plan

```
Phase 1: Hot Wallet MVP
  - Generate a dedicated bot wallet (new EOA, not a personal wallet)
  - Fund with ~0.01 ETH on Optimism (enough for thousands of transactions)
  - Store private key in hosting platform's encrypted env vars
  - Add web3.py to requirements.txt
  - Create a new utility module (e.g., utils/onchain.py) with submit_breakout()
  - Modify _post_submit_breakout in cogs/fractal/group.py to call it
  - Post tx hash + Optimism Etherscan link in Discord after submission
  - Add a /submit-onchain admin command as a manual fallback

Phase 2: Hardening (if needed later)
  - Move key to AWS KMS or similar HSM-backed signing
  - Add confirmation step: bot posts results, admin reacts to approve submission
  - Monitor wallet balance and alert if low
  - Rate-limit submissions (max 1 per group per fractal)
```

### Upgrade Path

If ZAO grows and the risk profile changes:

- **Medium scale (treasury > $10K, multiple admins):** Migrate to Gnosis Safe with bot as 1-of-2 signer. One admin confirms each submission.
- **Large scale (significant treasury, regulatory concerns):** Migrate to ERC-4337 Smart Account with session keys, or Turnkey for HSM-grade key management.

The hot wallet approach does not lock you in -- the `submit_breakout()` function can be swapped to use any signing backend later without changing the rest of the bot.

---

## Sources

- [web3.py Transactions Documentation](https://web3py.readthedocs.io/en/stable/transactions.html)
- [web3.py Accounts Documentation](https://web3py.readthedocs.io/en/stable/web3.eth.account.html)
- [Turnkey Transaction Automation](https://www.turnkey.com/transaction-automation)
- [Turnkey Wallets-as-a-Service API Guide](https://www.turnkey.com/blog/an-in-depth-guide-to-turnkeys-wallets-as-a-service-waas-api)
- [Agent Wallets Compared: Crossmint, Privy, Turnkey, Coinbase](https://www.crossmint.com/learn/agent-wallets-compared)
- [Privy Embedded Wallets 101](https://www.privy.io/embedded-wallets-101)
- [Fireblocks vs Privy vs Turnkey Comparison](https://www.fireblocks.com/report/compare-embedded-wallet-infrastructure)
- [safe-eth-py on PyPI](https://pypi.org/project/safe-eth-py/)
- [Safe Transaction Service Documentation](https://docs.safe.global/core-api/api-safe-transaction-service)
- [safe-eth-py GitHub Repository](https://github.com/safe-global/safe-eth-py)
- [ZeroDev Session Keys Documentation](https://docs.zerodev.app/sdk/advanced/session-keys)
- [ZeroDev Introduction](https://docs.zerodev.app/)
- [ERC-4337 Documentation](https://docs.erc4337.io/index.html)
- [Alchemy: What is Account Abstraction](https://www.alchemy.com/overviews/what-is-account-abstraction)
- [Collab.Land Zodiac Bot Module Proposal](https://hackmd.io/@Bau/SkpqOP9Wi)
- [Collab.Land Zodiac Bot Module -- Building in Public](https://mirror.xyz/wordsmiths.eth/gIH_cTVCGMc6BQ1Hy8cY5XYn5mxqhA5WVOeP-CmgXPo)
- [Optimystics/op-fractal-sc (Respect Contract)](https://github.com/Optimystics/op-fractal-sc)
- [Optimystics/frapps (Fractal Apps Toolkit)](https://github.com/Optimystics/frapps)
- [AWS KMS Ethereum Signing](https://aws.amazon.com/blogs/web3/make-eoa-private-keys-compatible-with-aws-kms/)
- [Azure Key Vault Ethereum Wallet Management](https://medium.com/microsoftazure/simple-ethereum-wallets-management-with-azure-key-vault-2b701bc0505)
