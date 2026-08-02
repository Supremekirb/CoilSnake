from coilsnake.exceptions.common.exceptions import CoilSnakeTraceableError
from coilsnake.model.eb.blocks import EbCompressibleBlock
from coilsnake.model.eb.graphics import EbGraphicTileset, EbOneByteTileArrangement, EbTileArrangement, EbOneByteTileArrangementItem
from coilsnake.model.eb.palettes import EbPalette
from coilsnake.model.eb.table import eb_table_from_offset
from coilsnake.modules.eb.EbModule import EbModule
from coilsnake.util.eb.pointer import from_snes_address, to_snes_address, AsmPointerReference, XlPointerReference
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

BATTLE_ANIMATION_TABLE_REFERENCES = (
    AsmPointerReference(0x02E34f),
    XlPointerReference(0x02E153),
    XlPointerReference(0x02E1BA),
    XlPointerReference(0x02E509),
)
BATTLE_ANIMATION_PALETTE_TABLE_REFERENCES = (
    AsmPointerReference(0x02E2F4),
)
BATTLE_ANIMATION_ARRANGEMENT_PTRS_REFERENCES = (
    AsmPointerReference(0x02E461),
)
BATTLE_ANIMATION_TILESETS_REFERENCES = (
    # Pointer to the bank which the compressed tilesets are in
    AsmPointerReference(0x02E13F),
    AsmPointerReference(0x02E1A6),
)

BATTLE_ANIMATIONS_BANK = 0xCC

# Common EbTileArrangement used to render the tileset to an image
TILESET_IMAGE_ARRANGEMENT = EbTileArrangement(16, 16)
tile_id_to_write = 0
for tile_row in TILESET_IMAGE_ARRANGEMENT.arrangement:
    for tile in tile_row:
        tile.tile = tile_id_to_write
        tile_id_to_write += 1
del tile_id_to_write

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
        
        if self.frame_count is None:
            # Happens when reading from project, because we don't know the frame count yet
            self.arrangements = []
        else:
            self.arrangements = [EbOneByteTileArrangement(width=32, height=32) for _ in range(self.frame_count)]
    
    def arrangements_from_block(self, block, offset):       
        with EbCompressibleBlock() as compressed_block:
            compressed_block.from_compressed_block(block, offset)
            next_offset = 0
            for frame in self.arrangements:
                frame.from_block(compressed_block, next_offset)
                next_offset += frame.block_size()
    
    def arrangements_to_block(self):
        size = sum(frame.block_size() for frame in self.arrangements)
        block = EbCompressibleBlock(size)
        next_offset = 0
        for frame in self.arrangements:
            frame.to_block(block, next_offset)
            next_offset += frame.block_size()
        block.compress()
        return block
    
    def arrangements_from_map(self, map_file):
        self.arrangements = []
        self.frame_count = 0
        raw: str = map_file.read()
        text_frames = raw.split("\n\n")
        
        for text_frame in text_frames[:-1]: # Last split is the trailing gap
            arrangement = EbOneByteTileArrangement(32, 32)
            
            rows = text_frame.split("\n")
            if len(rows) != 32:
                raise CoilSnakeTraceableError("Frame #{}: Incorrect number of rows. Expected 32, got {}".format(
                    self.frame_count, len(rows)))
            
            for y, row in enumerate(rows):
                if len(row) != 96: # 32 tiles of 3 chars each, 2 letters and a space
                    raise CoilSnakeTraceableError("Frame #{}: Incorrect number of columns. Expected 32, got {}".format(
                        self.frame_count, len(row)
                    ))
                for x, tile in enumerate(row.split()):
                    arrangement.arrangement[y][x] = EbOneByteTileArrangementItem(int(tile, 16))
                    arrangement.arrangement[y][x].check_validity()
            
            self.arrangements.append(arrangement)
            self.frame_count += 1
    

class BattleAnimationModule(EbModule):
    """Extracts battle animations from EarthBound"""
    NAME = "Battle Animations"
    
    # Animations config, arrangements, arrangement pointers, tilesets, and palettes
    FREE_RANGES = [(0x0C2E19, 0x0CF617)]
    
    def __init__(self):
        super(BattleAnimationModule, self).__init__()
        self.battle_animation_table = eb_table_from_offset(BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS,
                                                 hidden_columns=["Short pointer to tileset", "Frame count"])
        self.palette_table = eb_table_from_offset(BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS)
        self.arrangement_ptr_table = eb_table_from_offset(BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS)
        self.battle_animations: list[BattleAnimation] = []
        
        self.tilesets: list[EbGraphicTileset] = [] # List of tilesets, for deduplication purposes
        
    def read_from_rom(self, rom):
        self.battle_animation_table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS)
        )
        self.palette_table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS)
        )
        self.arrangement_ptr_table.from_block(
            rom, offset=from_snes_address(BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS)
        )
        
        known_tilesets = {} # Maps short addresses to indexes in self.tilesets
        
        for index in range(self.battle_animation_table.num_rows):
            row = self.battle_animation_table[index]            
            battle_animation = BattleAnimation(*row)
            
            battle_animation.palette = self.palette_table[index][0]
            
            arrangement_ptr = from_snes_address(self.arrangement_ptr_table[index][0])
            battle_animation.arrangements_from_block(rom, arrangement_ptr)
        
            tileset_ptr = from_snes_address(battle_animation.tileset_pointer_short + (BATTLE_ANIMATIONS_BANK << 16))
            # Keep track of tilesets we've already seen and skip duplicating them
            # Not as crucial here as it is when writing ...
            if not tileset_ptr in known_tilesets:
                with EbCompressibleBlock() as compressed_block:
                    tileset = EbGraphicTileset(256)
                    compressed_block.from_compressed_block(rom, tileset_ptr)
                    tileset.from_block(compressed_block)
                    known_tilesets[tileset_ptr] = len(self.tilesets)
                    self.tilesets.append(tileset)
                    
            battle_animation.tileset = self.tilesets[known_tilesets[tileset_ptr]]
            
            self.battle_animations.append(battle_animation)
    
    def write_to_rom(self, rom):
        # Write palette table
        palette_table_offset = rom.allocate(size=self.palette_table.size)
        self.palette_table.to_block(rom, palette_table_offset)
        # Relocate references to palette table
        for reference in BATTLE_ANIMATION_PALETTE_TABLE_REFERENCES:
            if reference.validate_structure(rom):
                reference.write(rom, to_snes_address(palette_table_offset))
            else:
                log.warning("Palette table relocation at %#x failed structure check - skipping...", reference.offset)
        
        # Write compressed arrangements
        # The arrangements for different animations can be in different banks. Lucky us     
        for id, animation in enumerate(self.battle_animations):
            block = animation.arrangements_to_block()
            arrangement_offset = rom.allocate(size=block.size)
            # Write to ROM
            rom.to_block(block, arrangement_offset)
            # Reconstruct table of arrangement pointers
            self.arrangement_ptr_table[id] = [to_snes_address(arrangement_offset)]
        
        # Write table of arrangement pointers
        arrangement_ptr_table_offset = rom.allocate(size=self.arrangement_ptr_table.size)
        self.arrangement_ptr_table.to_block(rom, arrangement_ptr_table_offset)
        # Relocate references to table of arrangement pointers
        for reference in BATTLE_ANIMATION_ARRANGEMENT_PTRS_REFERENCES:
            if reference.validate_structure(rom):
                reference.write(rom, to_snes_address(arrangement_ptr_table_offset))
            else:
                log.warning("Arrangement pointer table relocation at %#x failed structure check - skipping...", reference.offset)
        
        tilesets_compressed: list[EbCompressibleBlock] = []
        for tileset in self.tilesets:
            with EbCompressibleBlock(tileset.block_size()) as compressed_block:
                tileset.to_block(compressed_block)
                compressed_block.compress()
                tilesets_compressed.append(compressed_block)
        compressed_total_size = sum(i.size for i in tilesets_compressed)
        
        current_tileset_offset = rom.allocate(size=compressed_total_size)
        tileset_short_ptrs = []
        tilesets_bank_only = current_tileset_offset & 0xFF0000
        
        # Write the compressed tilesets
        for compressed in tilesets_compressed:
            # compressed.to_block(rom, current_tileset_offset)
            rom.to_block(compressed, current_tileset_offset)
            tileset_short_ptrs.append(current_tileset_offset & 0xFFFF)
            current_tileset_offset += compressed.size
        # Repoint them
        for reference in BATTLE_ANIMATION_TILESETS_REFERENCES:
            if reference.validate_structure(rom):
                reference.write(rom, to_snes_address(tilesets_bank_only))
            else:
                log.warning("Tileset bank relocation at %#x failed structure check - skipping...", reference.offset)

        
        # Write the config table. Fill in the final field: the short pointer to the tileset
        self.battle_animation_table.recreate(len(self.battle_animations))
        for id, animation in enumerate(self.battle_animations):
            # This is slower, but better than keeping an index into the self.tilesets list in the
            # BattleAnimation object, because that keeps it more separate.
            animation.tileset_pointer_short = tileset_short_ptrs[self.tilesets.index(animation.tileset)]
            # Now fill in the fields
            self.battle_animation_table[id] = [
                animation.tileset_pointer_short,
                animation.frame_duration,
                animation.palette_cycle_duration,
                animation.palette_cycle_lower_index,
                animation.palette_cycle_upper_index,
                animation.frame_count,
                animation.targetting,
                animation.enemy_color_delay,
                animation.enemy_color_duration,
                animation.enemy_color
            ]
        battle_animation_table_offset = rom.allocate(size=self.battle_animation_table.size)
        self.battle_animation_table.to_block(rom, battle_animation_table_offset)
        # And repoint
        for reference in BATTLE_ANIMATION_TABLE_REFERENCES:
            if reference.validate_structure(rom):
                reference.write(rom, to_snes_address(battle_animation_table_offset))
            else:
                log.warning("Battle animation table relocation at %#x failed structure check - skipping...", reference.offset)            
        
    def read_from_project(self, resource_open):
        with resource_open("BattleAnimations/battle_animations", "yml", True) as f:
            yml_rep = yml_load(f)
            num_rows = len(yml_rep)
            self.battle_animation_table.recreate(num_rows=num_rows)
            self.battle_animation_table.from_yml_rep(yml_rep)
            
        # Recreate the palette and arrangement tables too
        self.palette_table.recreate(num_rows=self.battle_animation_table.num_rows)
        self.arrangement_ptr_table.recreate(num_rows=self.battle_animation_table.num_rows)
        
        for animation_id in range(self.battle_animation_table.num_rows):
            try:
                data = self.battle_animation_table[animation_id]
                animation = BattleAnimation(*data)
                self.battle_animations.append(animation)
                # Frame count and short pointer to tileset still need to be filled in
                # We'll fill in frame count here after we read the map files,
                # and tileset pointer after we figure out where it's going in the ROM
                
                with resource_open("BattleAnimations/{:02d}/tileset".format(animation_id), "png") as tileset_f:
                    tileset_image = open_indexed_image(tileset_f)
                    
                    palette = EbPalette(1, 4)
                    palette.from_image(tileset_image)
                    animation.palette = palette
                    self.palette_table[animation_id] = [palette]
                    
                    # Dedup identical tilesets
                    known_tilesets = {}
                    tileset = EbGraphicTileset(256)
                    tileset.from_image(tileset_image, TILESET_IMAGE_ARRANGEMENT, palette)
                    if str(tileset.tiles) not in known_tilesets:
                        # Converting to a string so it's hashable and can be put in a dict.
                        # Probably stupid. Let me know if you have a better idea
                        known_tilesets[str(tileset.tiles)] = len(self.tilesets)
                        self.tilesets.append(tileset)
                    animation.tileset = self.tilesets[known_tilesets[str(tileset.tiles)]]
                
                with resource_open("BattleAnimations/{:02d}/arrangement".format(animation_id), "map", True) as map_f:
                    animation.arrangements_from_map(map_f) # Frame count is filled in now
                    # Will fill in the arrangement pointers when we actually have the arrangements in the ROM
                    
            except Exception as e:
                message = "Encountered an error while reading battle animation #{}.".format(animation_id)
                raise CoilSnakeTraceableError(message, e)
        
    def write_to_project(self, resource_open):
        for i, animation in enumerate(self.battle_animations):
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
                image = TILESET_IMAGE_ARRANGEMENT.image(animation.tileset, animation.palette, True)
                image.save(f, "png")
                
        with resource_open("BattleAnimations/battle_animations", "yml", True) as f:
            self.battle_animation_table.to_yml_file(f)
    
    def upgrade_project(self, old_version, new_version, rom, resource_open_r, resource_open_w, resource_delete):
        if old_version < 14:
            self.read_from_rom(rom)
            self.write_to_project(resource_open_w)