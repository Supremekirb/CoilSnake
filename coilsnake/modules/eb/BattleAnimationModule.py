from coilsnake.exceptions.common.exceptions import CoilSnakeTraceableError
from coilsnake.model.eb.blocks import EbCompressibleBlock
from coilsnake.model.eb.graphics import EbGraphicTileset, EbOneByteTileArrangement, EbTileArrangement
from coilsnake.model.eb.palettes import EbPalette
from coilsnake.model.eb.table import eb_table_from_offset
from coilsnake.modules.eb.EbModule import EbModule
from coilsnake.util.eb.pointer import from_snes_address, to_snes_address
from coilsnake.util.common.image import open_indexed_image
from coilsnake.util.common.yml import yml_dump, yml_load
from coilsnake.util.common.type import enum_class_from_name_list

import logging

log = logging.getLogger(__name__)

BATTLE_ANIMATION_TARGET = ["one", "row", "all", "random"]

BattleAnimationTargetEnum = enum_class_from_name_list(BATTLE_ANIMATION_TARGET)

BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS = 0xCCF04D
BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS = 0xCCF47F
BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS = 0xCCF58F

BATTLE_ANIMATIONS_BANK = 0xCC

class BattleAnimation:
    def __init__(self,
        tileset_pointer_short, frame_duration, palette_cycle_duration,
        palette_cycle_lower_index, palette_cycle_upper_index,
        frame_count, targetting, enemy_color_delay,
        enemy_color_duration, enemy_color
    ):
        self.tileset_pointer_short = tileset_pointer_short
        self.frame_duration = frame_duration
        self.palette_cycle_duration = palette_cycle_duration
        self.palette_cycle_lower_index = palette_cycle_lower_index
        self.palette_cycle_upper_index = palette_cycle_upper_index
        self.frame_count = frame_count
        self.targetting = targetting
        self.enemy_color_delay = enemy_color_delay
        self.enemy_color_duration = enemy_color_duration
        self.enemy_color = enemy_color
        
        self.palette = EbPalette(num_subpalettes=1, subpalette_length=4)
        self.tileset = EbGraphicTileset(num_tiles=256)
        self.arrangements = [EbOneByteTileArrangement(width=32, height=32) for _ in range(self.frame_count)]
    
    def arrangements_from_block(self, block, offset):       
        with EbCompressibleBlock() as compressed_block:
            compressed_block.from_compressed_block(block, offset)
            next_offset = 0
            for frame in self.arrangements:
                frame.from_block(compressed_block, next_offset)
                next_offset += frame.block_size()
    
    def tileset_from_block(self, block, offset):
        with EbCompressibleBlock() as compressed_block:
            compressed_block.from_compressed_block(block, offset)
            self.tileset.from_block(compressed_block)
    

class BattleAnimationModule(EbModule):
    """Extracts battle animations from EarthBound"""
    NAME = "Battle Animations"
    
    # Animations config, arrangements, arrangement pointers, tilesets, and palettes
    # FREE_RANGES = [(0x0C2E19, 0x0CF617)]
    
    def __init__(self):
        super(BattleAnimationModule, self).__init__()
        self.config_table = eb_table_from_offset(BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS)
        self.palette_table = eb_table_from_offset(BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS)
        self.arrangement_ptr_table = eb_table_from_offset(BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS)
        self.battle_animations: list[BattleAnimation] = []
        
    def read_from_rom(self, rom):
        self.config_table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS)
        )
        self.palette_table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS)
        )
        self.arrangement_ptr_table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS)
        )
        
        for index in range(self.config_table.num_rows):
            row = self.config_table[index]            
            battle_animation = BattleAnimation(*row)
            
            battle_animation.palette = self.palette_table[index][0]
            
            arrangement_ptr = from_snes_address(self.arrangement_ptr_table[index][0])
            battle_animation.arrangements_from_block(rom, arrangement_ptr)
            
            tileset_ptr = from_snes_address(battle_animation.tileset_pointer_short + (BATTLE_ANIMATIONS_BANK << 16))
            battle_animation.tileset_from_block(rom, tileset_ptr)
            
            self.battle_animations.append(battle_animation)
    
    def write_to_rom(self, rom):
        log.warning("Not implemented yet")
        # TODO this
        
    def read_from_project(self, resource_open):
        log.warning("Not implemented yet")
        # TODO this
        
    def write_to_project(self, resource_open):
        # Common EbTileArrangement used to render the tileset to an image
        tileset_arrangement = EbTileArrangement(16, 16)
        tile_id_to_write = 0
        for tile_row in tileset_arrangement.arrangement:
            for tile in tile_row:
                tile.tile = tile_id_to_write
                tile_id_to_write += 1

        animation_data = {}
        for i, animation in enumerate(self.battle_animations):
            # Organise yaml data
            animation_data[i] = {
                "Delay before enemy color change": animation.enemy_color_delay,
                "Duration of enemy color change": animation.enemy_color_duration,
                "Enemy color": animation.enemy_color.yml_rep(), # why doesn't this happen automatically?
                # "Frame count": animation.frame_count, # Infer from map file
                "Frame duration": animation.frame_duration,
                # "Palette": animation.palette.yml_rep(), # Infer from tileset file
                "Palette cycle duration": animation.palette_cycle_duration,
                "Palette cycle lower index": animation.palette_cycle_lower_index,
                "Palette cycle upper index": animation.palette_cycle_upper_index,
                "Targetting": BattleAnimationTargetEnum.tostring(animation.targetting),
            }
            
            # Write arrangements (tilemaps)
            # This is called ".map" just like the overworld map but it is a little different.
            # - Tile indexes are 2-digit instead of 3-digit (we can only have 256 tiles)
            # - The data is arranged into a series of rectangles representing a frame each
            # - Maybe we need a different file extension...
            with resource_open("BattleAnimations/{:02d}/arrangement".format(i), "map", True) as f:
                for frame in animation.arrangements:
                    for row in range(frame.height):
                        for col in range(frame.width):
                            f.write(hex(frame[col, row].tile)[2:].zfill(2))
                            f.write(" ")
                        f.write("\n")
                    f.write("\n")
            
            # Write tileset image                    
            with resource_open("BattleAnimations/{:02d}/tileset".format(i), "png") as f:
                image = tileset_arrangement.image(animation.tileset, animation.palette, True)
                image.save(f, "png")
                
        with resource_open("BattleAnimations/battle_animations", "yml", True) as f:
            yml_dump(animation_data, f, default_flow_style=False)
    
    def upgrade_project(self, old_version, new_version, rom, resource_open_r, resource_open_w, resource_delete):
        if old_version < 14:
            self.read_from_rom(rom)
            self.write_to_project(resource_open_w)