# Copyright (c) 2025 Benoît Pelletier
# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import aiosqlite
from datetime import datetime, timedelta
import os
from typing import Optional, Dict, List, Tuple
import json
import math
from dismob.rate_limiter import get_rate_limiter
from dismob import log, filehelper
from enum import Enum, auto

async def setup(bot: commands.Bot):
    log.info("Module `levels` setup")
    filehelper.ensure_directory("db")
    await bot.add_cog(LevelSystem(bot))

async def teardown(bot: commands.Bot):
    log.info("Module `levels` teardown")
    await bot.remove_cog("LevelSystem")

class LevelSystem(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot: commands.Bot = bot
        self.db_path = "db/levels.db"
        self.db_ready = False
        self.rate_limiter = get_rate_limiter()
        
        # Cache pour les cooldowns et temps vocal
        self.message_cooldowns = {}
        
    async def cog_load(self):
        self.config = filehelper.openConfig("levels")
        """Initialise la connexion à la base de données"""
        await self.setup_database()
        # Démarrer les tâches après l'initialisation de la DB
        if self.db_ready:
            log.info("Database is ready, starting voice_exp_task")
            self.voice_exp_task.start()
        
    async def cog_unload(self):
        filehelper.saveConfig(self.config, "levels")
        """Cleanup resources"""
        if hasattr(self, 'voice_exp_task'):
            log.info("Cancelling voice_exp_task")
            self.voice_exp_task.cancel()

    async def setup_database(self):
        """Configure la base de données SQLite locale et crée les tables"""
        try:
            log.info(f"Initializing SQLite database: {self.db_path}")
            
            # Créer la base de données et les tables
            async with aiosqlite.connect(self.db_path) as db:
                # Table des utilisateurs et niveaux
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_levels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        exp INTEGER DEFAULT 0,
                        level INTEGER DEFAULT 0,
                        total_messages INTEGER DEFAULT 0,
                        voice_time INTEGER DEFAULT 0,
                        welcome INTEGER DEFAULT 0,
                        last_message_time TEXT,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                
                # Table des récompenses obtenues
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_rewards (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        user_id INTEGER,
                        level_reached INTEGER,
                        role_id INTEGER,
                        obtained_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (guild_id, user_id) REFERENCES user_levels(guild_id, user_id) ON DELETE CASCADE
                    )
                """)
                
                # Créer les index pour les performances
                await db.execute("CREATE INDEX IF NOT EXISTS idx_user_levels_guild_user ON user_levels(guild_id, user_id)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_user_levels_exp ON user_levels(guild_id, exp DESC)")
                await db.execute("CREATE INDEX IF NOT EXISTS idx_user_rewards_guild_user ON user_rewards(guild_id, user_id)")
                
                await db.commit()
                
            log.info("SQLite database successfully initialized")
            self.db_ready = True
            
        except Exception as e:
            log.error(f"Error during database initialization: {e}")
            self.db_ready = False

    async def wait_for_db(self):
        """Attend que la base de données soit prête"""
        max_wait = 30
        waited = 0
        while not self.db_ready and waited < max_wait:
            await asyncio.sleep(1)
            waited += 1
        
        if not self.db_ready:
            log.warning("Timeout to wait for database")

    async def get_user_data(self, guild_id: int, user_id: int) -> Dict:
        """Récupère les données d'un utilisateur"""
        if not self.db_ready:
            return {'user_id': user_id, 'exp': 0, 'level': 0, 'total_messages': 0, 'voice_time': 0, 'welcome': 0}
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT user_id, exp, level, total_messages, voice_time, welcome FROM user_levels WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id,)
                )
                result = await cursor.fetchone()
                
                if result:
                    return {
                        'user_id': result[0],
                        'exp': result[1],
                        'level': result[2],
                        'total_messages': result[3],
                        'voice_time': result[4],
                        'welcome': result[5]
                    }
                else:
                    # Créer un nouvel utilisateur
                    await db.execute(
                        "INSERT INTO user_levels (guild_id, user_id, exp, level) VALUES (?, ?, 0, 0)",
                        (guild_id, user_id,)
                    )
                    await db.commit()
                    return {'user_id': user_id, 'exp': 0, 'level': 0, 'total_messages': 0, 'voice_time': 0, 'welcome': 0}
        except Exception as e:
            print(f"Erreur get_user_data: {e}")
            return {'user_id': user_id, 'exp': 0, 'level': 0, 'total_messages': 0, 'voice_time': 0, 'welcome': 0}

    class ExpGainType(Enum):
        MESSAGE = auto()
        VOICE = auto()
        WELCOME = auto()

        @staticmethod
        def from_str(label: str):
            label = label.lower()
            if label == "message":
                return LevelSystem.ExpGainType.MESSAGE
            elif label == "voice":
                return LevelSystem.ExpGainType.VOICE
            elif label == "welcome":
                return LevelSystem.ExpGainType.WELCOME
            else:
                raise ValueError(f"Unknown ExpGainType: {label}")

        def __str__(self):
            return self.name.lower()

        def __repr__(self):
            return f"ExpGainType.{self.name}"

        def __eq__(self, other):
            if isinstance(other, LevelSystem.ExpGainType):
                return self.value == other.value
            return False

        def __hash__(self):
            return hash(self.value)

        @classmethod
        def all(cls):
            return list(cls)

        @classmethod
        def choices(cls):
            return [e.name.lower() for e in cls]

        @classmethod
        def default(cls):
            return cls.MESSAGE

        @classmethod
        def is_valid(cls, value):
            return value in cls

        @classmethod
        def from_context(cls, context: str):
            if context == "voice":
                return cls.VOICE
            elif context == "welcome":
                return cls.WELCOME
            else:
                return cls.MESSAGE

        @classmethod
        def get_update_sql(cls, gain_type):
            if gain_type == cls.VOICE:
                return (
                    "UPDATE user_levels SET exp = ?, level = ?, voice_time = voice_time + 1, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?"
                )
            elif gain_type == cls.WELCOME:
                return (
                    "UPDATE user_levels SET exp = ?, level = ?, welcome = welcome + 1, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?"
                )
            else:
                return (
                    "UPDATE user_levels SET exp = ?, level = ?, total_messages = total_messages + 1, last_message_time = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?"
                )

        @classmethod
        def get_update_params(cls, gain_type, new_exp, new_level, guild_id, user_id):
            return (new_exp, new_level, guild_id, user_id)

        @classmethod
        def is_voice(cls, gain_type):
            return gain_type == cls.VOICE

        @classmethod
        def is_welcome(cls, gain_type):
            return gain_type == cls.WELCOME

        @classmethod
        def is_message(cls, gain_type):
            return gain_type == cls.MESSAGE

    async def update_user_exp(self, user: discord.Member, exp_gain: int, gain_type: ExpGainType = ExpGainType.MESSAGE):
        """Met à jour l'EXP d'un utilisateur et gère les montées de niveau"""
        if not self.db_ready:
            return 0, 0, 0

        try:
            user_data = await self.get_user_data(user.guild.id, user.id)
            old_level = user_data['level']
            new_exp = max(0, user_data['exp'] + exp_gain)
            new_level = self.calculate_level(new_exp)

            update_sql = LevelSystem.ExpGainType.get_update_sql(gain_type)
            update_params = LevelSystem.ExpGainType.get_update_params(gain_type, new_exp, new_level, user.guild.id, user.id)

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(update_sql, update_params)
                await db.commit()

            # Vérifier les récompenses de niveau (seulement si niveau augmenté)
            if new_level != old_level:
                asyncio.create_task(self.update_rewards(user, new_level))

            return old_level, new_level, exp_gain
        except Exception as e:
            log.error(f"Erreur update_user_exp: {e}")
            return 0, 0, 0

    def calculate_level(self, exp: int) -> int:
        """Calcule le niveau basé sur l'EXP (formule: 75*level²)"""
        if exp < 75:
            return 0
        # Résoudre l'équation: exp = 75 * level²
        # level = sqrt(exp / 75)
        return int(math.sqrt(exp / 75))

    def calculate_exp_for_level(self, level: int) -> int:
        """Calcule l'EXP requise pour un niveau (formule: 75*level²)"""
        return 75 * level * level

    def calculate_exp_from_activity(self, messages: int, voice_minutes: int) -> int:
        """Calcule l'EXP total basé sur les messages et temps vocal"""
        return (messages * self.config['exp_per_message']) + (voice_minutes * self.config['exp_per_voice_minute'])

    async def safe_add_role(self, member: discord.Member, role: discord.Role, reason: str = None):
        """Ajoute un rôle de manière sécurisée avec rate limiting"""
        try:
            await self.rate_limiter.execute_request(
                member.add_roles(role, reason=reason),
                route=f'PATCH /guilds/{member.guild.id}/members/{member.id}',
                major_params={'guild_id': member.guild.id}
            )
            log.info(f"The role '{role.name}' has been added to {member.display_name}")
            return True
        except discord.Forbidden:
            log.error(f"Bot has not the permission to add the role '{role.name}'")
            return False
        except discord.NotFound:
            log.error(f"Can't find role '{role.name}' or member '{member.display_name}'")
            return False
        except Exception as e:
            log.error(f"Error when adding role '{role.name}': {e}")
            return False

    async def safe_remove_role(self, member: discord.Member, role: discord.Role, reason: str = None):
        """Retire un rôle de manière sécurisée avec rate limiting"""
        try:
            await self.rate_limiter.execute_request(
                member.remove_roles(role, reason=reason),
                route=f'PATCH /guilds/{member.guild.id}/members/{member.id}',
                major_params={'guild_id': member.guild.id}
            )
            log.info(f"The role '{role.name}' has been removed from {member.display_name}")
            return True
        except discord.Forbidden:
            log.error(f"Bot has not the permission to add the role '{role.name}'")
            return False
        except discord.NotFound:
            log.error(f"Can't find role '{role.name}' or member '{member.display_name}'")
            return False
        except Exception as e:
            log.error(f"Error when removing role '{role.name}': {e}")
            return False

    async def announce_reward(self, member: discord.Member, level: int, rewards: Optional[List[Tuple[int, discord.Role]]] = None):
        """Annonce une récompense dans le channel niveaux"""
        try:
            level_channel_id = self.config.get('level_channel_id')
            if not level_channel_id:
                log.error("Level channel ID not configured")
                return
            
            try:
                channel = member.guild.get_channel(level_channel_id)
            except ValueError:
                log.error(f"Invalid rewards channel ID: {level_channel_id}")
                return
            
            if not channel:
                log.error(f"level channel {level_channel_id} not found in guild {member.guild.name}")
                return
            
            log.info(f"Level channel found: {channel.name}")
            
            rewards_message: dict = self.config.get('reward_messages', {})
            level_rewards: dict = self.config.get('level_rewards', {})

            # Préparer le message
            level_str = str(level)
            if level_str in rewards_message:
                message = rewards_message[level_str].format(user=member.mention)
                log.info(f"Message personnalisé: {message}")
            else:
                role_id = level_rewards.get(level_str)
                reward_str = f" et obtient le rôle <@&{role_id}>" if role_id else None
                message = f":tada: {member.mention} a atteint le **niveau {level}**{reward_str if reward_str else ''} !"
                log.info(f"Message par défaut: {message}")
            
            # Envoyer le message
            result = await log.safe_send_message(channel, message)
            if result:
                log.info(f"Annonce envoyée pour {member.display_name} niveau {level}")
            else:
                log.error(f"Échec envoi annonce pour {member.display_name} niveau {level}")
                
        except Exception as e:
            log.error(f"Erreur announce_reward: {e}")

    async def update_rewards(self, member: discord.Member, member_level: int):
        """Vérifie et attribue les récompenses de niveau"""
        if not member:
            return
        
        try:
            level_rewards: dict = self.config.get('level_rewards', {})
            remove_previous_rewards: bool = self.config.get('remove_previous_rewards', True)

            # Get all applicable roles for the new level
            applicable_roles: List[Tuple[int, int]] = []
            non_applicable_roles: List[Tuple[int, int]] = []
            for role_level_str, role_id in level_rewards.items():
                try:
                    role_level = int(role_level_str)
                except ValueError:
                    log.error(f"Invalid level in rewards config: {role_level_str}")
                    continue

                # Vérifier si le niveau est dans la plage
                if role_level <= member_level:
                    applicable_roles.append((role_level, role_id))
                else:
                    non_applicable_roles.append((role_level, role_id))

            # Get topmost level role
            topmost_role_id: Optional[int] = None
            highest_level: int = -1
            for role_level, role_id in applicable_roles:
                if role_level > highest_level:
                    highest_level = role_level
                    topmost_role_id = role_id

            # Get list of roles to add and remove
            roles_to_add: List[Tuple[int, discord.Role]] = []
            roles_to_remove: List[Tuple[discord.Role]] = []
            for role_level, role_id in applicable_roles:
                role = member.guild.get_role(role_id)
                if not role:
                    log.warning(f"Rôle {role_id} introuvable pour le niveau {role_level}")
                    continue
                
                if role not in member.roles:
                    if not remove_previous_rewards or role_id == topmost_role_id:
                        roles_to_add.append((role_level, role))
                elif remove_previous_rewards and role_id != topmost_role_id:
                    roles_to_remove.append((role_level, role))
                    
            for role_level, role_id in non_applicable_roles:
                role = member.guild.get_role(role_id)
                if role and role in member.roles:
                    roles_to_remove.append((role_level, role))

            # Attribution des rôles
            if self.db_ready:
                try:
                    async with aiosqlite.connect(self.db_path) as db:
                        for level, role in roles_to_add:
                            success = await self.safe_add_role(member, role, f"Niveau {level} atteint")
                            if success:
                                log.info(f"Rôle {role.name} attribué à {member.display_name} pour le niveau {level}")
                                
                                # Enregistrer la récompense
                                await db.execute(
                                    "INSERT OR IGNORE INTO user_rewards (guild_id, user_id, level_reached, role_id) VALUES (?, ?, ?, ?)",
                                    (member.guild.id, member.id, level, role.id)
                                )
                            else:
                                log.error(f"Échec attribution rôle {role.name} à {member.display_name}")

                        for level, role in roles_to_remove:
                            success = await self.safe_remove_role(member, role, "Récompense précédente remplacée")
                            if success:
                                log.info(f"Rôle {role.name} (récompense de niveau {level}) retiré de {member.display_name}")
                                
                                # Supprimer la récompense de la DB
                                await db.execute(
                                    "DELETE FROM user_rewards WHERE guild_id = ? AND user_id = ? AND level_reached = ? AND role_id = ?",
                                    (member.guild.id, member.id, level, role.id)
                                )

                        await db.commit()
                except Exception as e:
                    log.error(f"Erreur enregistrement récompense: {e}")
                
            # Annoncer la récompense
            await self.announce_reward(member, member_level, roles_to_add)
                            
        except Exception as e:
            log.error(f"Erreur update_rewards: {e}")

    def get_multiplier(self, member: discord.Member) -> float:
        """Calcule le multiplicateur d'EXP basé sur les rôles (additif)"""
        base_multiplier = 1.0
        bonus_multiplier = 0.0
        
        role_multipliers: dict = self.config.get('role_multipliers', {})

        # Additionner tous les bonus de multiplicateurs
        for role in member.roles:
            bonus_multiplier += role_multipliers.get(role.id, 0.0)
        
        return base_multiplier + bonus_multiplier

    def is_admin(self, user: discord.Member) -> bool:
        """Vérifie si l'utilisateur est admin"""
        if user.guild_permissions.administrator:
            return True
        admin_roles = self.config.get('admin_roles', [])
        return any(role.id in admin_roles for role in user.roles)

    async def display_level_info(self, interaction: discord.Interaction, utilisateur: Optional[discord.Member] = None):
        """Fonction partagée pour afficher les informations de niveau"""
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible. Le système de niveaux est temporairement indisponible.")
            return
        
        target = utilisateur or interaction.user
        user_data = await self.get_user_data(interaction.guild.id, target.id)
        
        current_level = user_data['level']
        current_exp = user_data['exp']
        exp_for_current = self.calculate_exp_for_level(current_level)
        exp_for_next = self.calculate_exp_for_level(current_level + 1)
        exp_progress = current_exp - exp_for_current
        exp_needed = exp_for_next - exp_for_current
        
        # Créer l'embed
        embed = discord.Embed(
            title=f"📊 Profil de {target.display_name}",
            color=discord.Color.blurple()
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="🎯 Niveau", value=f"`{current_level}`", inline=True)
        embed.add_field(name="⭐ EXP Total", value=f"`{current_exp:,}`", inline=True)
        embed.add_field(name="📈 Progression", value=f"`{exp_progress:,}/{exp_needed:,}`", inline=True)
        embed.add_field(name="💬 Messages", value=f"`{user_data['total_messages']:,}`", inline=True)
        embed.add_field(name="🎤 Temps Vocal", value=f"`{user_data['voice_time']:,}` min", inline=True)
        
        # Barre de progression
        progress_bar_length = 20
        progress = min(exp_progress / exp_needed, 1.0) if exp_needed > 0 else 1.0
        filled_length = int(progress_bar_length * progress)
        bar = "█" * filled_length + " " * (progress_bar_length - filled_length)
        embed.add_field(name="📊 Progression vers le niveau suivant", value=f"`{bar}` {progress*100:.1f}%", inline=False)
        
        # Prochaine récompense
        level_rewards: dict = self.config.get('level_rewards', {})
        next_reward_level: Optional[int] = None
        for level_str in sorted(level_rewards.keys()):
            level: int = int(level_str)
            if level > current_level:
                next_reward_level = level
                break
        
        if next_reward_level:
            role_id = level_rewards.get(str(next_reward_level))
            role = interaction.guild.get_role(role_id)
            role_mention = role.mention if role else f"<@&{role_id}>"
            embed.add_field(name="🎁 Prochaine Récompense", value=f"Niveau {next_reward_level}: {role_mention}", inline=False)
        
        embed.set_footer(text=f"Serveur: {interaction.guild.name}")
        await log.safe_respond(interaction, embed=embed)

    async def display_leaderboard(self, interaction: discord.Interaction, page: Optional[int] = 1):
        """Fonction partagée pour afficher le classement"""
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible. Le système de niveaux est temporairement indisponible.")
            return
        
        page = max(1, page)
        offset = (page - 1) * 10
        
        try:
            # Récupérer les données du leaderboard
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT user_id, exp, level FROM user_levels WHERE guild_id = ? ORDER BY exp DESC LIMIT 10 OFFSET ?",
                    (interaction.guild.id, offset,)
                )
                results = await cursor.fetchall()
                
                # Compter le total d'utilisateurs
                cursor = await db.execute("SELECT COUNT(*) FROM user_levels WHERE guild_id = ?", (interaction.guild.id,))
                total_users = (await cursor.fetchone())[0]
        except Exception as e:
            await log.failure(interaction, "Erreur lors de la récupération des données.", ephemeral=True)
            return
        
        if not results:
            embed = discord.Embed(
                title="📋 Classement des Niveaux",
                description="Aucun utilisateur trouvé pour cette page.",
                color=discord.Color.red()
            )
            await log.safe_respond(interaction, embed=embed)
            return
        
        # Créer l'embed
        embed = discord.Embed(
            title="🏆 Classement des Niveaux",
            color=discord.Color.gold()
        )
        
        description = ""
        for i, (user_id, exp, level) in enumerate(results, start=offset + 1):
            user = self.bot.get_user(user_id)
            user_name = user.display_name if user else f"Utilisateur {user_id}"
            
            # Emojis pour le podium
            if i == 1:
                emoji = "🥇"
            elif i == 2:
                emoji = "🥈"
            elif i == 3:
                emoji = "🥉"
            else:
                emoji = f"`{i}.`"
            
            description += f"{emoji} **{user_name}** - Niveau `{level}` (`{exp:,}` EXP)\n"
        
        embed.description = description
        
        # Informations de pagination
        max_pages = math.ceil(total_users / 10) if total_users > 0 else 1
        embed.set_footer(text=f"Page {page}/{max_pages} • {total_users} utilisateurs au total")
        
        await log.safe_respond(interaction, embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Donne de l'EXP pour les messages"""
        if message.author.bot or not message.guild:
            return
        
        # Attendre que la DB soit prête
        if not self.db_ready:
            return
        
        # Vérifier si le channel est blacklisté
        blacklisted_channels = self.config.get('blacklisted_channels', [])
        if message.channel.id in blacklisted_channels:
            return
        
        user_id = message.author.id
        current_time = datetime.now()
        
        # Vérifier le cooldown
        if user_id in self.message_cooldowns:
            time_diff = (current_time - self.message_cooldowns[user_id]).total_seconds()
            if time_diff < self.config.get('message_cooldown', 0):
                return
        
        # Calculer l'EXP avec multiplicateur
        base_exp = self.config.get('exp_per_message', 0)
        multiplier = self.get_multiplier(message.author)
        final_exp = int(base_exp * multiplier)
        
        # Mettre à jour l'EXP (les récompenses sont gérées dans update_user_exp -> update_rewards)
        old_level, new_level, exp_gained = await self.update_user_exp(message.author, final_exp)
        
        # Mettre à jour le cooldown
        self.message_cooldowns[user_id] = current_time

    @tasks.loop(minutes=1)
    async def voice_exp_task(self):
        """Donne de l'EXP aux utilisateurs en vocal chaque minute (seulement si pas seuls)"""
        if not self.db_ready:
            return
        
        log.debug("EXP task running...")
        
        blacklisted_channels = self.config.get('blacklisted_channels', [])
        
        for guild in self.bot.guilds:
            log.debug(f"Traitement des channels vocaux pour le serveur {guild.name} ({guild.id})")
            # Maybe will have different settings later for each guild
            base_exp: int = self.config.get('exp_per_voice_minute', 0)
            for voice_channel in guild.voice_channels:
                log.debug(f"- Traitement du channel vocal {voice_channel.name} ({voice_channel.id})")
                if voice_channel.id in blacklisted_channels:
                    log.debug(f"  - Channel blacklisté")
                    continue

                active_members = [m for m in voice_channel.members if not m.bot and not m.voice.self_deaf]
                if len(active_members) < 2:
                    log.debug(f"  - Pas assez de membres actifs")
                    continue

                for member in active_members:
                    # Calculer l'EXP vocal avec multiplicateur
                    multiplier = self.get_multiplier(member)
                    final_exp = int(base_exp * multiplier)

                    log.debug(f"  - Donne {final_exp} EXP à {member.display_name} dans le channel vocal {voice_channel.name}")
                    
                    # Mettre à jour l'EXP
                    await self.update_user_exp(member, final_exp, from_voice=True)

    @voice_exp_task.before_loop
    async def before_voice_exp_task(self):
        await self.bot.wait_until_ready()
        await self.wait_for_db()

    # Slash Commands
    @app_commands.command(name="niveau", description="Affiche tes informations de niveau")
    async def level_info(self, interaction: discord.Interaction, utilisateur: Optional[discord.Member] = None):
        """Affiche les informations de niveau d'un utilisateur"""
        await self.display_level_info(interaction, utilisateur)

    @app_commands.command(name="level", description="Affiche tes informations de niveau")
    async def level_alias(self, interaction: discord.Interaction, utilisateur: Optional[discord.Member] = None):
        """Alias pour /niveau"""
        await self.display_level_info(interaction, utilisateur)

    @app_commands.command(name="classement", description="Affiche le classement des niveaux")
    async def leaderboard_fr(self, interaction: discord.Interaction, page: Optional[int] = 1):
        """Affiche le leaderboard avec pagination"""
        await self.display_leaderboard(interaction, page)

    @app_commands.command(name="leaderboard", description="Affiche le classement des niveaux")
    async def leaderboard(self, interaction: discord.Interaction, page: Optional[int] = 1):
        """Alias pour /classement"""
        await self.display_leaderboard(interaction, page)

    @app_commands.command(name="toplevel", description="Affiche le classement des niveaux")
    async def toplevel(self, interaction: discord.Interaction, page: Optional[int] = 1):
        """Alias pour /classement"""
        await self.display_leaderboard(interaction, page)

    # groupes de commandes exp
    expGroup = discord.app_commands.Group(name="xp", description="Commandes liées à l'EXP")

    @expGroup.command(name="add", description="Ajoute de l'EXP à un utilisateur (Admin)")
    @app_commands.describe(utilisateur="L'utilisateur à qui ajouter de l'EXP", montant="Montant d'EXP à ajouter")
    async def add_exp(self, interaction: discord.Interaction, utilisateur: discord.Member, montant: int):
        """Ajoute de l'EXP à un utilisateur (commande admin)"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if montant <= 0:
            await log.failure(interaction, "Le montant doit être positif.")
            return
        
        old_level, new_level, _ = await self.update_user_exp(utilisateur, montant)
        
        embed = discord.Embed(
            title="✅ EXP Ajoutée",
            description=f"**{montant:,}** EXP ajoutée à {utilisateur.mention}",
            color=discord.Color.green()
        )
        
        if new_level > old_level:
            embed.add_field(name="📈 Niveau", value=f"{old_level} → {new_level}", inline=False)
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)

    @expGroup.command(name="remove", description="Retire de l'EXP à un utilisateur (Admin)")
    @app_commands.describe(utilisateur="L'utilisateur à qui retirer de l'EXP", montant="Montant d'EXP à retirer")
    async def remove_exp(self, interaction: discord.Interaction, utilisateur: discord.Member, montant: int):
        """Retire de l'EXP à un utilisateur (commande admin)"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if montant <= 0:
            await log.failure(interaction, "Le montant doit être positif.")
            return
        
        user_data = await self.get_user_data(interaction.guild.id, utilisateur.id)
        if user_data['exp'] < montant:
            await log.failure(interaction, "L'utilisateur n'a pas assez d'EXP.")
            return
        
        old_level, new_level, _ = await self.update_user_exp(utilisateur, -montant)
        
        # Synchroniser les récompenses après modification manuelle avec annonces si niveau baisse
        if new_level < old_level:
            #asyncio.create_task(self.sync_user_rewards(utilisateur, announce=True))
            asyncio.create_task(self.update_rewards(utilisateur, new_level))
            
        
        embed = discord.Embed(
            title="✅ EXP Retirée",
            description=f"**{montant:,}** EXP retirée à {utilisateur.mention}",
            color=discord.Color.orange()
        )
        
        if new_level < old_level:
            embed.add_field(name="📉 Niveau", value=f"{old_level} → {new_level}", inline=False)
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)

    @expGroup.command(name="set", description="Définit l'EXP d'un utilisateur (Admin)")
    @app_commands.describe(utilisateur="L'utilisateur dont modifier l'EXP", montant="Nouveau montant d'EXP")
    async def set_exp(self, interaction: discord.Interaction, utilisateur: discord.Member, montant: int):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if montant < 0:
            await log.failure(interaction, "Le montant ne peut pas être négatif.")
            return
        
        try:
            user_data = await self.get_user_data(interaction.guild.id, utilisateur.id)
            old_level = user_data['level']
            new_level = self.calculate_level(montant)
            
            # Mettre à jour directement
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE user_levels SET exp = ?, level = ?, updated_at = CURRENT_TIMESTAMP WHERE guild_id = ? AND user_id = ?",
                    (montant, new_level, interaction.guild.id, utilisateur.id)
                )
                await db.commit()
            
            # Synchroniser toutes les récompenses avec annonces
            asyncio.create_task(self.update_rewards(utilisateur, new_level))
            
            embed = discord.Embed(
                title="✅ EXP Définie",
                description=f"EXP de {utilisateur.mention} définie à **{montant:,}**",
                color=discord.Color.blue()
            )
            embed.add_field(name="📊 Niveau", value=f"{old_level} → {new_level}", inline=False)
            
            await log.safe_respond(interaction, embed=embed, ephemeral=True)
        except Exception as e:
            await log.failure(interaction, "Erreur lors de la mise à jour: {e}", ephemeral=True)

    @app_commands.command(name="xp-set-activity", description="Définit l'activité d'un utilisateur et calcule l'EXP (Admin)")
    @app_commands.describe(
        utilisateur="L'utilisateur dont modifier l'activité",
        messages="Nombre de messages",
        temps_vocal="Temps vocal en minutes"
    )
    async def set_activity(self, interaction: discord.Interaction, utilisateur: discord.Member, messages: Optional[int] = None, temps_vocal: Optional[int] = None):
        """Définit l'activité d'un utilisateur et recalcule l'EXP"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if messages < 0 or temps_vocal < 0:
            await log.failure(interaction, "Les valeurs ne peuvent pas être négatives.")
            return
        
        if messages is None and temps_vocal is None:
            await log.failure(interaction, "Au moins un des paramètres doit être spécifié (messages ou temps vocal).")
            return
        
        try:
            # Récupérer les données actuelles
            user_data = await self.get_user_data(interaction.guild.id, utilisateur.id)
            old_level = user_data['level']
            old_messages = user_data['total_messages']
            old_voice_time = user_data['voice_time']

            if (messages is None):
                messages = old_messages

            if (temps_vocal is None):
                temps_vocal = old_voice_time

            # Calculer l'EXP total basé sur l'activité
            total_exp = self.calculate_exp_from_activity(messages, temps_vocal)
            new_level = self.calculate_level(total_exp)
            
            # Mettre à jour la base de données
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """UPDATE user_levels 
                       SET exp = ?, level = ?, total_messages = ?, voice_time = ?, updated_at = CURRENT_TIMESTAMP 
                       WHERE guild_id = ? AND user_id = ?""",
                    (total_exp, new_level, messages, temps_vocal, interaction.guild.id, utilisateur.id)
                )
                await db.commit()
            
            # Nettoyer les anciennes récompenses pour forcer une resynchronisation complète
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM user_rewards WHERE guild_id = ? AND user_id = ?", (interaction.guild.id, utilisateur.id,))
                await db.commit()
            
            embed = discord.Embed(
                title="✅ Activité Définie",
                description=f"Activité de {utilisateur.mention} mise à jour",
                color=discord.Color.blue()
            )
            embed.add_field(name="💬 Messages", value=f"`{messages:,}`", inline=True)
            embed.add_field(name="🎤 Temps Vocal", value=f"`{temps_vocal:,}` min", inline=True)
            embed.add_field(name="⭐ EXP Calculée", value=f"`{total_exp:,}`", inline=True)
            embed.add_field(name="📊 Niveau", value=f"{old_level} → {new_level}", inline=False)
            #embed.add_field(name="ℹ️ Note", value="Utilisez `/sync-rewards` pour synchroniser les récompenses", inline=False)
            
            await log.safe_respond(interaction, embed=embed)
        except Exception as e:
            await log.failure(interaction, "Erreur lors de la mise à jour.")

    @app_commands.command(name="toggle-remove-previous", description="Active/désactive la suppression des récompenses précédentes (Admin)")
    async def toggle_remove_previous(self, interaction: discord.Interaction):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        self.config['remove_previous_rewards'] = not self.config.get('remove_previous_rewards', True)
        status = "activée" if self.config['remove_previous_rewards'] else "désactivée"
        await log.success(interaction, f"La suppression des récompenses précédentes est maintenant **{status}**")

    @app_commands.command(name="level-debug", description="Informations de debug pour le système de niveau (Admin)")
    async def level_debug(self, interaction: discord.Interaction, utilisateur: Optional[discord.Member] = None):
        """Commande de debug pour vérifier l'état du système"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        embed = discord.Embed(
            title="🔧 Debug - Système de Niveaux",
            color=discord.Color.blurple()
        )
        
        # État de la base de données
        db_status = "✅ Connectée" if self.db_ready else "❌ Déconnectée"
        embed.add_field(name="Base de Données", value=db_status, inline=True)
        
        # Fichier de base de données
        embed.add_field(name="Fichier DB", value=f"`{self.db_path}`", inline=True)
        
        # Taille du fichier
        try:
            db_size = os.path.getsize(self.db_path) / 1024  # KB
            embed.add_field(name="Taille DB", value=f"`{db_size:.1f} KB`", inline=True)
        except:
            embed.add_field(name="Taille DB", value="`N/A`", inline=True)
        
        # Configuration
        remove_prev = "✅ Activée" if self.config.get('remove_previous_rewards', False) else "❌ Désactivée"
        embed.add_field(name="Suppression Précédentes", value=remove_prev, inline=True)
        
        # Cache
        #embed.add_field(name="Utilisateurs en vocal", value=f"`{len(self.voice_times)}`", inline=True)
        embed.add_field(name="Cooldowns actifs", value=f"`{len(self.message_cooldowns)}`", inline=True)
        
        # Tâches
        voice_task_status = "✅ Active" if hasattr(self, 'voice_exp_task') and not self.voice_exp_task.is_being_cancelled() else "❌ Inactive"
        embed.add_field(name="Tâche Vocal", value=voice_task_status, inline=True)
        
        # Rate limiter stats
        metrics = self.rate_limiter.get_metrics()
        embed.add_field(name="Rate Limiter", value=f"Req: {metrics['total_requests']}\nRL: {metrics['rate_limited_requests']}", inline=True)
        
        # Channel niveaux
        level_channel_id = self.config.get('level_channel_id')
        if level_channel_id:
            channel = interaction.guild.get_channel(int(level_channel_id))
            channel_status = f"✅ {channel.mention}" if channel else "❌ Introuvable"
        else:
            channel_status = "❌ Non configuré"
        embed.add_field(name="Channel Niveaux", value=channel_status, inline=True)
        
        # Debug utilisateur spécifique
        if utilisateur:
            user_data = await self.get_user_data(interaction.guild.id, utilisateur.id)
            embed.add_field(name=f"Debug {utilisateur.display_name}", 
                          value=f"Niveau: {user_data['level']}\nEXP: {user_data['exp']:,}", 
                          inline=False)
            
            # Vérifier les rôles actuels
            user_roles = [role.name for role in utilisateur.roles if role.id in self.config.get('level_rewards', {}).values()]
            embed.add_field(name="Rôles de niveau actuels", 
                          value=", ".join(user_roles) if user_roles else "Aucun", 
                          inline=False)
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)

    # Command group pour la gestion des salons blacklistés
    
    blackListGroup = discord.app_commands.Group(name="xp-blacklist-channel", description="Gestion des salons blacklistés pour l'EXP")

    @blackListGroup.command(name="list", description="Liste les salons blacklistés pour l'EXP")
    async def blacklist_list_channels(self, interaction: discord.Interaction):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        blacklisted_channels = self.config.get('blacklisted_channels')
        if not blacklisted_channels or len(blacklisted_channels) == 0:
            await log.client(interaction, "Aucun salon n'est actuellement blacklisté pour l'EXP.", title="Salons Blacklistés")
            return
        
        channels = [interaction.guild.get_channel(cid) for cid in blacklisted_channels]
        channel_mentions = [channel.mention for channel in channels if channel]
        
        if not channel_mentions:
            msg = "Aucun salon blacklisté trouvé."
        else:
            msg = "\n".join(channel_mentions)
        
        await log.client(interaction, msg, title="Salons Blacklistés")
    
    @blackListGroup.command(name="add", description="Blacklist un salon, les membres ne recevront pas d'xp en postant dans ce salon")
    async def blacklist_add_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        blacklisted_channels = self.config.get('blacklisted_channels', [])
        if channel.id in blacklisted_channels:
            await log.failure(interaction, f"Le salon {channel.mention} est déjà blacklisté.")
            return
        
        # Ajouter le salon à la liste des salons blacklistés
        blacklisted_channels.append(channel.id)
        self.config['blacklisted_channels'] = blacklisted_channels
        await log.success(interaction, f"Le salon {channel.mention} a été blacklisté avec succès.")

    @blackListGroup.command(name="remove", description="Retire un salon de la blacklist, les membres recevront à nouveau de l'xp en postant dans ce salon")
    async def blacklist_remove_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        blacklisted_channels = self.config.get('blacklisted_channels', [])
        if channel.id not in blacklisted_channels:
            await log.failure(interaction, f"Le salon {channel.mention} n'est pas dans la liste des salons blacklistés.")
            return
        
        # Retirer le salon de la liste des salons blacklistés
        blacklisted_channels.remove(channel.id)
        self.config['blacklisted_channels'] = blacklisted_channels
        await log.success(interaction, f"Le salon {channel.mention} a été retiré de la blacklist avec succès.")

    # Group to show or set xp settings
    xpSettingsGroup = discord.app_commands.Group(name="xp-settings", description="Gestion des paramètres d'EXP")

    @xpSettingsGroup.command(name="show", description="Affiche les paramètres d'EXP actuels")
    async def show_xp_settings(self, interaction: discord.Interaction):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        embed = discord.Embed(
            title="⚙️ Paramètres d'EXP Actuels",
            color=discord.Color.blurple()
        )
        
        embed.add_field(name="EXP par message", value=f"`{self.config.get('exp_per_message')}`", inline=True)
        embed.add_field(name="EXP par minute vocal", value=f"`{self.config.get('exp_per_voice_minute')}`", inline=True)
        embed.add_field(name="Cooldown pour les messages", value=f"`{self.config.get('message_cooldown')}s`", inline=True)
        
        await log.safe_respond(interaction, embed=embed)

    @xpSettingsGroup.command(name="set", description="Modifie les paramètres d'EXP")
    @app_commands.describe(
        exp_per_message="EXP gagnée par message envoyé",
        exp_per_voice_minute="EXP gagnée par minute passée en vocal",
        cooldown="Cooldown avant de donner de l'EXP pour un message (en secondes, 0 pour désactiver)"
    )
    async def set_xp_settings(self, interaction: discord.Interaction, exp_per_message: Optional[int] = None, 
                              exp_per_voice_minute: Optional[int] = None, cooldown: Optional[int] = None):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if exp_per_message is not None:
            if exp_per_message < 0:
                await log.failure(interaction, "L'EXP par message ne peut pas être négative.")
                return
            self.config['exp_per_message'] = exp_per_message
        
        if exp_per_voice_minute is not None:
            if exp_per_voice_minute < 0:
                await log.failure(interaction, "L'EXP par minute en vocal ne peut pas être négative.")
                return
            self.config['exp_per_voice_minute'] = exp_per_voice_minute
        
        if cooldown is not None:
            if cooldown < 0:
                await log.failure(interaction, "Le cooldown des messages doit être positif ou nul.")
                return
            self.config['message_cooldown'] = cooldown
        
        await log.success(interaction, "Les paramètres d'EXP ont été mis à jour avec succès.")
    
    # Group to show or set xp settings
    roleMultiplierGroup = discord.app_commands.Group(name="xp-role-multiplier", description="Gestion des paramètres d'EXP")

    # Role multiplier commands
    @roleMultiplierGroup.command(name="set", description="Gère les multiplicateurs d'EXP des rôles")
    @app_commands.describe(
        role="Le rôle à configurer",
        multiplier="Le multiplicateur d'EXP pour ce rôle (ex. 0.5 pour +50% d'xp, 0 pour retirer le multiplicateur)"
    )
    async def role_multiplier_set(self, interaction: discord.Interaction, role: discord.Role, multiplier: float):
        """Gère les multiplicateurs d'EXP des rôles"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if multiplier < 0:
            await log.failure(interaction, "Le multiplicateur ne peut pas être négatif.")
            return
        
        role_multipliers = self.config.get('role_multipliers', {})
        
        role_id: str = str(role.id)
        if multiplier == 0:
            # Si le multiplicateur est 0, on supprime le rôle de la configuration
            if role_id in role_multipliers:
                del role_multipliers[role_id]
                self.config['role_multipliers'] = role_multipliers
                await log.success(interaction, f"Le multiplicateur pour {role.mention} a été retiré.")
                return
            else:
                await log.failure(interaction, f"Le rôle {role.mention} n'a pas de multiplicateur défini.")
                return

        # Mettre à jour le multiplicateur dans la configuration
        role_multipliers[role_id] = multiplier
        self.config['role_multipliers'] = role_multipliers
        await log.success(interaction, f"Le multiplicateur pour {role.mention} est maintenant **+{100*multiplier:.1f}%**")

    @roleMultiplierGroup.command(name="list", description="Liste les multiplicateurs d'EXP des rôles")
    async def role_multiplier_list(self, interaction: discord.Interaction):
        """Liste les multiplicateurs d'EXP des rôles"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        role_multipliers: dict = self.config.get('role_multipliers')
        if not role_multipliers or len(role_multipliers) == 0:
            await log.client(interaction, "Aucun multiplicateur d'EXP n'est défini pour les rôles.", title="⚙️ Multiplicateurs d'EXP des Rôles")
            return
        
        embed = discord.Embed(
            title="⚙️ Multiplicateurs d'EXP des Rôles",
            color=discord.Color.blurple(),
            description = ""
        )
        
        for role_id, multiplier in role_multipliers.items():
            role = interaction.guild.get_role(int(role_id))
            if role:
                embed.description += f"{role.mention}\t**+{100*multiplier:.1f}%**\n"
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)

    # Level Rewards Management
    levelRewardsGroup = discord.app_commands.Group(name="xp-level-rewards", description="Gestion des récompenses de niveau")

    @levelRewardsGroup.command(name="set", description="Définit les récompenses de niveau pour un niveau spécifique")
    @app_commands.describe(
        niveau="Le niveau pour lequel définir la récompense",
        role="Le rôle à attribuer pour ce niveau"
    )
    async def set_level_reward(self, interaction: discord.Interaction, niveau: int, role: discord.Role):
        """Définit les récompenses de niveau pour un niveau spécifique"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if niveau < 1:
            await log.failure(interaction, "Le niveau doit être supérieur ou égal à 1.")
            return
        
        # Mettre à jour la récompense de niveau
        level_rewards = self.config.get('level_rewards', {})
        level_rewards[str(niveau)] = role.id
        self.config['level_rewards'] = level_rewards
        await log.success(interaction, f"Récompense de niveau **{niveau}** définie avec le rôle {role.mention}.")

    @levelRewardsGroup.command(name="remove", description="Supprime un rôle de récompense de niveau")
    @app_commands.describe(niveau="Le niveau dont supprimer la récompense")
    async def remove_level_reward(self, interaction: discord.Interaction, niveau: int):
        """Supprime un rôle de récompense de niveau"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        level_rewards = self.config.get('level_rewards', {})
        niveau_str = str(niveau)
        if niveau_str not in level_rewards:
            await log.failure(interaction, f"Aucune récompense définie pour le niveau **{niveau}**.")
            return
        
        # Retirer la récompense de niveau
        del level_rewards[niveau_str]
        self.config['level_rewards'] = level_rewards
        await log.success(interaction, f"Récompense de niveau **{niveau}** supprimée.")

    @levelRewardsGroup.command(name="list", description="Liste les récompenses de niveau définies")
    async def list_level_rewards(self, interaction: discord.Interaction):
        """Liste les récompenses de niveau définies"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        level_rewards: dict = self.config.get('level_rewards')
        if not level_rewards or len(level_rewards) == 0:
            await log.client(interaction, "Aucune récompense de niveau définie.", title="Récompenses de Niveau")
            return
        
        embed = discord.Embed(
            title="Récompenses de Niveau",
            color=discord.Color.blurple()
        )
        
        for niveau, role_id in sorted(level_rewards.items()):
            role = interaction.guild.get_role(role_id)
            if role:
                embed.add_field(name=f"Niveau {niveau}", value=role.mention, inline=False)
            else:
                embed.add_field(name=f"Niveau {niveau}", value="`Rôle introuvable`", inline=False)
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)

    # Rewards messages management
    rewardsMessagesGroup = discord.app_commands.Group(name="xp-rewards-messages", description="Gestion des messages de récompense")

    @rewardsMessagesGroup.command(name="set", description="Définit le message de récompense pour un niveau spécifique")
    @app_commands.describe(
        niveau="Le niveau pour lequel définir le message de récompense",
        message="Le message de récompense à envoyer"
    )
    async def set_rewards_message(self, interaction: discord.Interaction, niveau: int, message: str = None):
        """Définit le message de récompense pour un niveau spécifique"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if niveau < 1:
            await log.failure(interaction, "Le niveau doit être supérieur ou égal à 1.")
            return
        
        niveau_str: str = str(niveau)
        rewards_messages: dict = self.config.get('rewards_messages', {})
        if message is None or message.strip() == "":
            del rewards_messages[niveau_str]
        else:
            rewards_messages[niveau_str] = message
        self.config['rewards_messages'] = rewards_messages
        await log.success(interaction, f"Message de récompense pour le niveau **{niveau}** défini.")

    @rewardsMessagesGroup.command(name="list", description="Liste les messages de récompense définis")
    async def list_rewards_messages(self, interaction: discord.Interaction):
        """Liste les messages de récompense définis"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        rewards_messages = self.config.get('rewards_messages')
        if not rewards_messages:
            await log.client(interaction, "Aucun message de récompense défini.", title="Messages de Récompense")
            return
        
        embed = discord.Embed(
            title="Messages de Récompense",
            color=discord.Color.blurple()
        )
        
        for niveau, message in sorted(rewards_messages.items()):
            embed.add_field(name=f"Niveau {niveau}", value=message, inline=False)
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)

    #salon de niveaux
    @rewardsMessagesGroup.command(name="channel", description="Définit le salon où envoyer les messages de récompense de niveau")
    @app_commands.describe(channel="Le salon où envoyer les messages de récompense de niveau")
    async def set_rewards_channel(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        """Définit le salon où envoyer les messages de récompense de niveau"""
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        if channel is None:
            # Si aucun salon n'est spécifié, on affiche le salon actuel
            level_channel_id = self.config.get('level_channel_id')
            if not level_channel_id:
                await log.client(interaction, "Aucun salon de niveaux défini.", title="Salon de Niveaux")
                return
            
            channel = interaction.guild.get_channel(int(level_channel_id))
            if channel:
                await log.client(interaction, f"Le salon de niveaux actuel est {channel.mention}.", title="Salon de Niveaux")
            else:
                await log.client(interaction, f"Salon de niveaux introuvable (id: `{level_channel_id}`).", title="Salon de Niveaux")
            return
        
        # Mettre à jour le salon de niveaux
        self.config['level_channel_id'] = channel.id
        await log.success(interaction, f"Le salon de niveaux a été défini sur {channel.mention}.")

    # Level Manager Roles Commands
    adminRolesGroup = discord.app_commands.Group(name="xp-manager-roles", description="Gestion des rôles administrateurs")

    @adminRolesGroup.command(name="add", description="Ajoute un rôle gestionnaire de niveaux")
    @app_commands.describe(role="Le rôle à ajouter")
    async def add_admin_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        manager_roles = self.config.get('level_manager_roles', [])
        if role.id in manager_roles:
            await log.failure(interaction, f"Le rôle {role.mention} est déjà un rôle gestionnaire de niveaux.")
            return
        
        manager_roles.append(role.id)
        self.config['level_manager_roles'] = manager_roles
        await log.success(interaction, f"Le rôle {role.mention} a été ajouté comme gestionnaire de niveaux.")

    @adminRolesGroup.command(name="remove", description="Supprime un rôle gestionnaire de niveaux")
    @app_commands.describe(role="Le rôle à supprimer")
    async def remove_admin_role(self, interaction: discord.Interaction, role: discord.Role):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        manager_roles = self.config.get('level_manager_roles')
        if not manager_roles:
            await log.failure(interaction, "Aucun rôle gestionnaire de niveaux défini.")
            return
        
        if role.id not in manager_roles:
            await log.failure(interaction, f"Le rôle {role.mention} n'est pas un rôle gestionnaire de niveaux.")
            return
        
        self.config['level_manager_roles'].remove(role.id)
        await log.success(interaction, f"Le rôle {role.mention} a été retiré des rôles gestionnaire de niveaux.")

    @adminRolesGroup.command(name="list", description="Liste les rôles gestionnaire de niveaux")
    async def list_admin_roles(self, interaction: discord.Interaction):
        if not self.is_admin(interaction.user):
            await log.failure(interaction, "Tu n'as pas la permission d'utiliser cette commande.")
            return
        
        if not self.db_ready:
            await log.failure(interaction, "Base de données non disponible.")
            return
        
        manager_roles = self.config.get('level_manager_roles')
        if not manager_roles or len(manager_roles) == 0:
            await log.client(interaction, "Aucun rôle gestionnaire de niveaux défini.", title="Rôles Gestionnaire de Niveaux")
            return
        
        embed = discord.Embed(
            title="Rôles Gestionnaire de Niveaux",
            color=discord.Color.blurple()
        )
        
        for role_id in manager_roles:
            role = interaction.guild.get_role(role_id)
            if role:
                embed.add_field(name=f"ID: `{role_id}`", value=role.mention, inline=True)
            else:
                embed.add_field(name=f"ID: `{role_id}`", value="Rôle introuvable", inline=True)
        
        await log.safe_respond(interaction, embed=embed, ephemeral=True)
