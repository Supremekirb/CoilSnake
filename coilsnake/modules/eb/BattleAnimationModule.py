from coilsnake.exceptions.common.exceptions import CoilSnakeTraceableError
from coilsnake.model.common.ips import IpsPatch
from coilsnake.model.eb.blocks import EbCompressibleBlock
from coilsnake.model.eb.graphics import EbGraphicTileset, EbOneByteTileArrangement, EbTileArrangement, EbOneByteTileArrangementItem
from coilsnake.model.eb.palettes import EbPalette
from coilsnake.model.eb.table import eb_table_from_offset
from coilsnake.modules.common.PatchModule import get_ips_filename
from coilsnake.modules.eb.EbModule import EbModule
from coilsnake.util.eb.pointer import from_snes_address, to_snes_address, AsmPointerReference, XlPointerReference
from coilsnake.util.common.image import open_indexed_image
from coilsnake.util.common.yml import yml_load

import logging

log = logging.getLogger(__name__)

DEFAULT_ANIMATION_COUNT = 34

BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS = 0xCCF04D
BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS = 0xCCF47F
BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS = 0xCCF58F
BATTLE_ANIMATIONS_TILESET_BANK_DEFAULT_ADDRESS = 0xCC0000

BATTLE_ANIMATION_TABLE_REFERENCES = (
    AsmPointerReference(0x02E34F),
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
        block = EbCompressibleBlock(32*32*len(self.arrangements))
        next_offset = 0
        for frame in self.arrangements:
            frame.to_block(block, next_offset)
            next_offset += 32*32
        block.compress()
        return block
    
    def arrangements_from_map(self, map_file):
        self.arrangements = []
        self.frame_count = 0
        raw: str = map_file.read()
        text_frames = raw.split("\n\n")[:-1] # Last split is the trailing gap
        
        for text_frame in text_frames:
            arrangement = EbOneByteTileArrangement(32, 32)
            arrangement.arrangement = [[EbOneByteTileArrangementItem(int(x, 16)) for x in y.split()] for y in text_frame.split("\n")]
            self.arrangements.append(arrangement)
            self.frame_count += 1
            
        if self.frame_count > 64:
            raise Exception("Frame count cannot exceed 64 frames.")
    

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
        # Before this module was added, PSI animations were expanded manually
        # and applied to a base ROM for future compilations.
        # That means we need to read the code to find pointers to potentially relocated
        # tables, and do some guesswork (fancy programmers call it "heuristics") to find the length.
        config_ptr = BATTLE_ANIMATION_TABLE_REFERENCES[0].read(rom)
        if not config_ptr:
            log.warning("Code-read battle animation table reference starting at ${:06X} was invalid, defaulting to vanilla address".format(BATTLE_ANIMATION_TABLE_REFERENCES[0].offset))
            config_ptr = BATTLE_ANIMATION_TABLE_DEFAULT_ADDRESS
        
        palettes_ptr = BATTLE_ANIMATION_PALETTE_TABLE_REFERENCES[0].read(rom)
        if not palettes_ptr:
            log.warning("Code-read battle animation palettes reference starting at ${:06X} was invalid, defaulting to vanilla address".format(BATTLE_ANIMATION_PALETTE_TABLE_REFERENCES[0].offset))
            palettes_ptr = BATTLE_ANIMATION_PALETTES_DEFAULT_ADDRESS

        arrangements_ptrs_ptr = BATTLE_ANIMATION_ARRANGEMENT_PTRS_REFERENCES[0].read(rom)
        if not arrangements_ptrs_ptr:
            log.warning("Code-read battle animation arrangement pointer table reference starting at ${:06X} was invalid, defaulting to vanilla address".format(BATTLE_ANIMATION_ARRANGEMENT_PTRS_REFERENCES[0].offset))
            arrangements_ptrs_ptr = BATTLE_ANIMATION_ARRANGEMENT_PTRS_DEFAULT_ADDRESS
        
        tilesets_bank_ptr = BATTLE_ANIMATION_TILESETS_REFERENCES[0].read(rom)
        if not tilesets_bank_ptr or tilesets_bank_ptr & 0xFFFF != 0: # Pointer should only include the bank byte
            log.warning("Code-read battle animation tileset bank reference starting at ${:06X} was invalid, defaulting to vanilla address".format(BATTLE_ANIMATION_TILESETS_REFERENCES[0].offset))
            tilesets_bank_ptr = BATTLE_ANIMATIONS_TILESET_BANK_DEFAULT_ADDRESS
            
        log.info("Found battle animation pointers:\n  Config table: ${:06X}\n  Palettes: ${:06X}\n  Arrangement ptrs: ${:06X}\n  Tileset bank: ${:06X}".format(
            config_ptr, palettes_ptr, arrangements_ptrs_ptr, tilesets_bank_ptr
        ))
        
        # Now we need to guess the length of things.
        guessed_length = 0
        while True:
            # Keep going until any of these happen:
            # - Arrangement ptr is invalid, null, or goes over bank boundary
            # - Config table goes over bank boundary
            # - Decompressed arrangement data is not a multiple of the frame size
            # - Decompressed tileset length is incorrect
            # Other things which could be added:
            # - Check for data overlap
            
            # Check arrangement pointer table
            arrangement_ptr_ptr = arrangements_ptrs_ptr + guessed_length*self.arrangement_ptr_table.schema.size
            arrangement_ptr = rom.read_multi(from_snes_address(arrangement_ptr_ptr), 4)
            if arrangement_ptr_ptr & 0xFF0000 != arrangements_ptrs_ptr & 0xFF0000:
                # Bank cross
                log.info("Battle animation count stopping at {} due to arrangement pointer table bank cross".format(guessed_length))
                break
            if arrangement_ptr == 0 or arrangement_ptr & 0xFF000000:
                # Null or upper byte is set which is invalid
                log.info("Battle animation count stopping at {} due to invalid arrangement pointer ${:06X}".format(guessed_length, arrangement_ptr))
                break
            
            # Check config table
            cfg_table_entry = config_ptr + guessed_length*self.battle_animation_table.schema.size
            if cfg_table_entry & 0xFF0000 != config_ptr & 0xFF0000:
                # Bank cross
                log.info("Battle animation count stopping at {} due to config table bank cross".format(guessed_length))
                break
            # Check decomp'd arrangement data is a multiple of the frame size
            # Can't do framecount because Rockin G has 10 unused frames and the Switch Online ROM trims off a couple frames on Counter-PSI Unit
            with EbCompressibleBlock() as arrangement_block:
                arrangement_block.from_compressed_block(rom, from_snes_address(arrangement_ptr))
                if arrangement_block.size % 1024: # Sizeof a frame of decomp'd data
                    log.info("Battle animation count stopping at {} due to arrangement data not being a multiple of frame size".format(guessed_length))
                    break
            
            guessed_length += 1
        
        # Recreate the tables at the correct size
        self.battle_animation_table.recreate(num_rows=guessed_length)
        self.palette_table.recreate(num_rows=guessed_length)
        self.arrangement_ptr_table.recreate(num_rows=guessed_length)
        
        # Actually dump the data
        self.battle_animation_table.from_block(
            rom, offset=from_snes_address(config_ptr)
        )
        self.palette_table.from_block(
            rom, offset=from_snes_address(palettes_ptr)
        )
        self.arrangement_ptr_table.from_block(
            rom, offset=from_snes_address(arrangements_ptrs_ptr)
        )
        
        # Translate to internal format
        known_tilesets = {} # Maps short addresses to indexes in self.tilesets
        
        for index in range(self.battle_animation_table.num_rows):
            row = self.battle_animation_table[index]            
            battle_animation = BattleAnimation(*row)
            
            battle_animation.palette = self.palette_table[index][0]
            
            arrangement_ptr = from_snes_address(self.arrangement_ptr_table[index][0])
            battle_animation.arrangements_from_block(rom, arrangement_ptr)
        
            tileset_ptr = from_snes_address(battle_animation.tileset_pointer_short | tilesets_bank_ptr)
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
        # If necessary, apply the battle animation expansion patch.
        # The range this patch covers is not currently marked as free, so it's OK to use.
        # But if that ever changes then this may break.
        # (Ranges: 3F98D - 3F98F, 3FE00 - 3FE20)
        if len(self.battle_animations) > DEFAULT_ANIMATION_COUNT:
            # The patch causes battle animations #34 and up, when called via script (or the function at C3F981),
            # to require being called starting at ID 55 (56 in CCScript) instead of ID 34.
            # This is because battle animations and the HDMA-based enemy-attack animations share the same ID space.
            log.info("Applying battle animation expansion patch")
            patch = IpsPatch()
            patch.load(get_ips_filename(rom.type, "battle_animation_expand"))
            patch.apply(rom)           
        
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
        
        # For deduplication
        known_tilesets = {}
        tilesets_max_ID_used = []
        
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
                    tileset = EbGraphicTileset(256)
                    tileset.from_image(tileset_image, TILESET_IMAGE_ARRANGEMENT, palette)
                    tileset_hash = tileset.hash()
                    if tileset_hash not in known_tilesets.keys():
                        known_tilesets[tileset_hash] = len(self.tilesets)
                        self.tilesets.append(tileset)
                        tilesets_max_ID_used.append(0) # Populate later
                    animation.tileset = self.tilesets[known_tilesets[tileset_hash]]
                
                with resource_open("BattleAnimations/{:02d}/arrangement".format(animation_id), "map", True) as map_f:
                    animation.arrangements_from_map(map_f) # Frame count is filled in now
                    # Will fill in the arrangement pointers when we actually have the arrangements in the ROM
                    
                    # Find max tile ID used
                    tilesets_max_ID_used[known_tilesets[tileset_hash]] = max(
                        tilesets_max_ID_used[known_tilesets[tileset_hash]], 
                        max(max(max(tile.tile for tile in row) for row in arrangement.arrangement) for arrangement in animation.arrangements)
                        )
                    
            except Exception as e:
                message = "Encountered an error while reading battle animation #{}.".format(animation_id)
                raise CoilSnakeTraceableError(message, e)
        
        # Trim tilesets past max ID
        for index, tileset in enumerate(self.tilesets):
            tileset.num_tiles_maximum = tilesets_max_ID_used[index]+1
            tileset.tiles = tileset.tiles[:tileset.num_tiles_maximum]
        
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
    
    def upgrade_project(self, old_version, new_version, rom, old_compiled_rom, resource_open_r, resource_open_w, resource_delete):
        if old_version < 14:
            self.read_from_rom(old_compiled_rom)
            # We use the old compiled ROM because, in CoilSnake projects prior to the addition of this module,
            # typically battle animation repointing happens via CCScript at compile-time.
            self.write_to_project(resource_open_w)