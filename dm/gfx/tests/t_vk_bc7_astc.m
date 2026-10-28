// DM_VK_BC7_ASTC=1, for a RomFS whose BC7 textures scripts/romfs_bc7_astc.py transcoded to ASTC 4x4 (Hades on iPhones without BC): the game's BC7 images and views are
// ASTC 4x4 textures, the same 16 bytes per 4x4 block, so image sizes and buffer copies stay as the game computes them. Without the switch they stay BC7.
// Run both ways by check_t_vk_bc7_astc
#include "../vk/hle_vk.m"
int dm_guest_as_contains(uintptr_t address) { (void)address; return 0; }
void *front_metal_layer(void) { return NULL; }
module_t *module_of(const void *p) { (void)p; return NULL; }
module_t *g_mod; int g_nmod;
void svc_thread_dump(void) {}
uint64_t now_ns(void) { return clock_gettime_nsec_np(CLOCK_UPTIME_RAW); }
void dm_vi_layer_crop(int c[4]) { c[0] = c[1] = c[2] = c[3] = 0; }
int dm_opmode(void) { return 1; }
static int checks, fails;
#define CHECK(c, msg) do { checks++; if (!(c)) { fails++; fprintf(stderr, "FAIL: %s\n", msg); } } while (0)

int main(void) {
    const char *sw = getenv("DM_VK_BC7_ASTC"); int astc = sw && !strcmp(sw, "1");
    VkDevice dev = &g_device;
    @autoreleasepool {
        dev_init();
        struct { VkFormat vk; MTLPixelFormat bc7, astc; } cases[] = {
            {VK_FORMAT_BC7_UNORM_BLOCK, MTLPixelFormatBC7_RGBAUnorm, MTLPixelFormatASTC_4x4_LDR},
            {VK_FORMAT_BC7_SRGB_BLOCK, MTLPixelFormatBC7_RGBAUnorm_sRGB, MTLPixelFormatASTC_4x4_sRGB},
        };
        for (size_t i = 0; i < sizeof cases / sizeof *cases; i++) {
            MTLPixelFormat want = astc ? cases[i].astc : cases[i].bc7;
            VkFormatProperties fp; h_vkGetPhysicalDeviceFormatProperties(NULL, cases[i].vk, &fp);
            CHECK(fp.optimalTilingFeatures & VK_FORMAT_FEATURE_SAMPLED_IMAGE_BIT, "BC7 is reported sampleable");
            VkImageCreateInfo ici = {.sType = VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO, .imageType = VK_IMAGE_TYPE_2D, .format = cases[i].vk, .extent = {8, 12, 1},
                                     .mipLevels = 1, .arrayLayers = 1, .samples = VK_SAMPLE_COUNT_1_BIT, .tiling = VK_IMAGE_TILING_OPTIMAL, .usage = VK_IMAGE_USAGE_SAMPLED_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT};
            VkImage im = NULL; CHECK(h_vkCreateImage(dev, &ici, NULL, &im) == VK_SUCCESS && im, "BC7 image created");
            if (!im) continue;
            CHECK(im->tex.pixelFormat == want, astc ? "BC7 image is an ASTC 4x4 texture" : "BC7 image is a BC7 texture");
            CHECK(im->fmt == cases[i].vk, "the image keeps the game's format");
            CHECK(im->size == 2 * 3 * 16, "6 blocks of 16 bytes, as BC7");
            VkImageViewCreateInfo vci = {.sType = VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO, .image = im, .viewType = VK_IMAGE_VIEW_TYPE_2D, .format = cases[i].vk,
                                         .subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1}};
            VkImageView view = NULL; CHECK(h_vkCreateImageView(dev, &vci, NULL, &view) == VK_SUCCESS && view && view->tex.pixelFormat == want, "its view has the image's format");
            if (view) h_vkDestroyImageView(dev, view, NULL);
            h_vkDestroyImage(dev, im, NULL);
        }
    }
    printf("Vulkan BC7 as ASTC (DM_VK_BC7_ASTC %s): %d checks, %d failed\n", astc ? "on" : "off", checks, fails);
    return fails != 0;
}
