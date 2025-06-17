# Level System for Dismob

This is a [dismob](https://github.com/dismob/dismob) plugin which adds a level system to the bot.

Members gain experience points by posting messages and being in voice channels.  
Other plugins can also add experience points when doing some events.

## Installation

> [!IMPORTANT]
> You need to have an already setup [dismob](https://github.com/dismob/dismob) bot. Follow the instruction there to do it first.

Just download/clone (or add as submodule) this repo into your dismob's `plugins` folder.  
The path **must** be `YourBot/plugins/levels/main.py` at the end.

Once your bot is up and live, run those commands on your discord server:

```
!modules load levels
!sync
```

> [!NOTE]
> Replace the prefix `!` by your own bot prefix when doing those commands!

Then you can reload your discord client with `Ctrl+R` to see the new slash commands.

## Commands

Command | Description
--- | ---
`/level [member]` | Display the level information of the specified member, or the member using the command if not specified.
`/leaderboard [page]` | Display the leaderboard. The page can be specified.
`/xp add <member> <amount>` | Add an xp amount to a specified member.
`/xp remove <member> <amount>` | Remove an xp amount to a specified user.
`/xp set <member> <amount>` | Set the amount of xp of a specified member.
`/xp-set-activity <member> [message] [voice_time]` | Compute the amount of xp for a member based on the activity specified.
`/toggle-previous-reward` | .
`/level-debug [member]` | .
`/xp-blacklist-channel list` | List channel that will not provide experience points.
`/xp-blacklist-channel add <channel>` | Add channel to blacklist.
`/xp-blacklist-channel remove <channel>` | Remove channel from blacklist.
`/xp-settings show` | .
`/xp-settings set [xp_per_message] [xp_per_voice_minute] [cooldown]` | .
`/xp-role-multiplier list` | .
`/xp-role-multiplier set <@role> <multiplier>` | .
`/xp-level-rewards list` | .
`/xp-level-rewards set <level> <@role>` | .
`/xp-level-rewards remove <level>` | .
`/xp-rewards-messages list` | .
`/xp-rewards-messages set <level> <message>` | .
`/xp-rewards-messages channel [channel]` | .
`/xp-manager-roles list` | .
`/xp-manager-roles add <@role>` | .
`/xp-manager-roles remove <@role>` | .
