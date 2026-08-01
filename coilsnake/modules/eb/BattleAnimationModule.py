from coilsnake.exceptions.common.exceptions import CoilSnakeTraceableError
from coilsnake.model.eb.blocks import EbCompressibleBlock
from coilsnake.model.eb.graphics import EbGraphicTileset, EbTileArrangement
from coilsnake.model.eb.palettes import EbPalette
from coilsnake.model.eb.table import eb_table_from_offset
from coilsnake.modules.eb.EbModule import EbModule
from coilsnake.util.eb.pointer import from_snes_address, to_snes_address
from coilsnake.util.common.image import open_indexed_image
from coilsnake.util.common.yml import yml_dump, yml_load

import logging

log = logging.getLogger(__name__)

BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS = 0xCCF04D

BATTLE_ANIMATIONS_BANK = 0x0C

class BattleAnimation:
    def __init__(self,
        tileset_pointer_short, frame_duration, palette_cycle_duration,
        palette_cycle_lower_index, palette_cycle_upper_index,
        frame_count, targetting, enemy_colour_delay,
        enemy_colour_duration, enemy_colour
    ):
        self.tileset_pointer_short = tileset_pointer_short
        self.frame_duration = frame_duration
        self.palette_cycle_duration = palette_cycle_duration
        self.palette_cycle_lower_index = palette_cycle_lower_index
        self.palette_cycle_upper_index = palette_cycle_upper_index
        self.frame_count = frame_count
        self.targetting = targetting
        self.enemy_colour_delay = enemy_colour_delay
        self.enemy_colour_duration = enemy_colour_duration
        self.enemy_colour = enemy_colour
        
        self.palette = EbPalette(num_subpalettes=1, subpalette_length=4)
        self.graphics = EbGraphicTileset(num_tiles=256)
        self.arrangements = [EbTileArrangement(width=32, height=28) for _ in range(self.frame_count)]
        
    # def from_block(self, block, offset):
    #     with EbCompressibleBlock() as compressed_block:
            
    

class BattleAnimationModule(EbModule):
    """Extracts battle animations from EarthBound"""
    NAME = "Battle Animations"
    
    # Animations config, arrangements, arrangement pointers, tilesets, and palettes
    # FREE_RANGES = [(0x0C2E19, 0x0CF617)]
    
    def __init__(self):
        super(BattleAnimationModule, self).__init__()
        self.table = eb_table_from_offset(BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS)
        self.battle_animations: list[BattleAnimation] = []
        
    def read_from_rom(self, rom):
        self.table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS)
        )
        
        for index in range(self.table.num_rows):
            row = self.table[index]
            tileset_pointer_short, frame_duration, palette_cycle_duration,\
            palette_cycle_lower_index, palette_cycle_upper_index,\
            frame_count, targetting, enemy_colour_delay,\
            enemy_colour_duration, enemy_colour = row
            
            battle_animation = BattleAnimation(row[0], row[1], row[2], row[3],
                                               row[4], row[5], row[6], row[7],
                                               row[8], row[9])
            
            self.battle_animations.append(battle_animation)
    
    def write_to_rom(self, rom):
        log.warning("Not implemented yet")
        # TODO this
        
    def read_from_project(self, resource_open):
        log.warning("Not implemented yet")
        # TODO this
        
    def write_to_project(self, resource_open):
        animation_data = {}
        for i, animation in enumerate(self.battle_animations):
            animation_data[i] = {
                "Frame duration": animation.frame_duration,
                "Palette cycle duration": animation.palette_cycle_duration,
                "Palette cycle lower index": animation.palette_cycle_lower_index,
                "Palette cycle upper index": animation.palette_cycle_upper_index,
                # "Frame count": animation.frame_count, # Infer from data?
                "Targetting": animation.targetting,
                "Delay before enemy colour change": animation.enemy_colour_delay,
                "Duration of enemy colour change": animation.enemy_colour_duration,
                # "Enemy colour": animation.enemy_colour, # Need yaml rep
            }
            # TODO rest of data dump. Images, maps, etc
            with resource_open("BattleAnimations/battle_animations", "yml", True) as f:
                yml_dump(animation_data, f, default_flow_style=False)
    
    def upgrade_project(self, old_version, new_version, rom, resource_open_r, resource_open_w, resource_delete):
        if old_version < 14:
            self.read_from_rom(rom)
            self.write_to_project(resource_open_w)