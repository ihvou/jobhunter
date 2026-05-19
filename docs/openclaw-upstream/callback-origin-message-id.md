# Telegram callback prompts should expose the source message_id

## Summary

OpenClaw Telegram callback handling currently routes an inline-button tap to the agent as a synthetic user message whose effective `messageId` is the Telegram `callback_query.id`, not the original Telegram message that contained the button.

For agents that need to delete or edit the message that had the tapped button, this makes the natural follow-up fail: `message(action="delete", messageId=<synthetic message id>)` targets the callback id, not the button message id.

## Environment

- Image: `ghcr.io/openclaw/openclaw:2026.5.7-slim`
- Channel: Telegram
- Agent runtime: Codex app-server
- Reproduced with Jobhunter inline cards on Telegram DM.

## Repro Steps

1. Start OpenClaw 2026.5.7 with Telegram enabled.
2. Have an agent send a Telegram message with inline buttons:

   ```json
   {
     "action": "send",
     "target": "telegram:<chat_id>",
     "message": "Card text",
     "presentation": {
       "blocks": [
         {
           "type": "buttons",
           "buttons": [
             { "text": "Applied", "callback_data": "applied:abc123abc123" }
           ]
         }
       ]
     }
   }
   ```

3. Tap the inline button in Telegram.
4. Let the agent handle the synthetic callback prompt and call:

   ```json
   {
     "action": "delete",
     "target": "telegram:<chat_id>",
     "messageId": "<message id available on the synthetic callback prompt>"
   }
   ```

## Actual Behavior

The delete/edit does not target the original Telegram card. Telegram rejects the operation because the identifier is the callback query id, not `callback.message.message_id`.

Observed source evidence in the image:

- `/app/extensions/telegram/src/bot-handlers.runtime.ts:1794-1801` builds the synthetic message and calls `processMessage(..., { messageIdOverride: callback.id })`.
- `/app/extensions/telegram/src/bot-handlers.runtime.ts:1423-1426` already has access to `callbackMessage.message_id` while constructing callback context metadata.
- `/app/extensions/telegram/src/send.ts:604` builds normal message `reply_markup` from inline-button data only; the subsequent delete/edit path needs the actual Telegram message id supplied by the callback flow.

Relevant snippet from the image:

```ts
const syntheticMessage = buildSyntheticTextMessage({
  base: withResolvedTelegramForumFlag(callbackMessage, isForum),
  from: callback.from,
  text: nativeCallbackCommand ?? data,
});
await processMessage(buildSyntheticContext(ctx, syntheticMessage), [], storeAllowFrom, {
  ...(nativeCallbackCommand ? { commandSource: "native" as const } : {}),
  forceWasMentioned: true,
  messageIdOverride: callback.id,
});
```

## Expected Behavior

The agent should have a reliable way to target the message that contained the tapped inline button.

Either of these APIs would solve it:

1. Include `callback_origin_message_id` (or similar) in the synthetic callback prompt metadata, set to `callback.message.message_id`.
2. Add a message action shortcut such as:

   ```json
   {
     "action": "delete-callback-source"
   }
   ```

   This would delete the source message from the implicit callback context without requiring the agent to copy a message id around.

## Current Workaround

Jobhunter now uses a two-call rendering pattern:

1. Send the card with placeholder callback data, e.g. `pending:<id_prefix>`.
2. Capture the Telegram `messageId` returned by `message(action="send")`.
3. Immediately edit the same card and replace callbacks with data that embeds the original message id, e.g. `applied:<id_prefix>:<messageId>`.
4. On callback, parse `messageId` out of `callback_data`, call the domain tool, then call `message(action="delete", messageId=<messageId>)`.

This works, but it doubles Telegram message API calls for every card and makes otherwise-simple callback flows more fragile.

## Why This Matters

Inline-card UIs commonly want the tapped card to disappear or update in place after a successful action. Without the original source `message_id`, agents either:

- leave stale cards in chat,
- send extra confirmation messages that clutter the conversation, or
- encode Telegram message ids into callback data as an application-level workaround.

Exposing the callback source message id would let agents handle callback UIs cleanly and retire the two-call workaround.
