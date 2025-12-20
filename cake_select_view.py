# cake_select_view.py
import discord

# ---- Your product data (edit to match your real 21 items) ----

CAKE_SIZES = ["Mini", '3"', '6"', '8"']

CAKE_TYPES = ["Madeleine", "Financier"]  # extend as needed

CAKE_PRODUCTS = {
    "Madeleine": [
        "薄荷朱古力瑪德蓮",
        "雙重朱古力瑪德蓮",
        # ... add more madeleines
    ],
    "Financier": [
        "經典牛油費南雪",
        "開心果費南雪",
        # ... add more financiers
    ],
}


class CakeOrderView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=600)  # 10 minutes
        self.user_id = user_id
        self.selected_size: str | None = None
        self.selected_type: str | None = None
        self.selected_product: str | None = None
        self.cart: list[tuple[str, str, str]] = []  # (size, type, product)

    # ---------- Helper to update message ----------

    async def update_message(self, interaction: discord.Interaction):
        """Update embed with current selections and cart."""
        size = self.selected_size or "(not selected)"
        type_ = self.selected_type or "(not selected)"
        product = self.selected_product or "(not selected)"

        lines = [
            f"Size: {size}",
            f"Type: {type_}",
            f"Product: {product}",
            "",
        ]

        if self.cart:
            lines.append("🛒 Cart:")
            for i, (s, t, p) in enumerate(self.cart, 1):
                lines.append(f"{i}. {s} / {t} / {p}")
        else:
            lines.append("🛒 Cart: (empty)")

        embed = discord.Embed(
            title="🎂 Cake Order System",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )

        try:
            await interaction.message.edit(embed=embed, view=self)
        except Exception:
            # If original message not accessible (rare), ignore
            pass

    # ---------- Size Select ----------

    @discord.ui.select(
        placeholder="Step 1: Select Size",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label=size, value=size) for size in CAKE_SIZES
        ],
        row=0,
    )
    async def select_size(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This menu is not for you.", ephemeral=True
            )
            return

        self.selected_size = select.values[0]
        # Reset lower-level selections
        self.selected_type = None
        self.selected_product = None

        await interaction.response.defer()
        await self.update_message(interaction)

    # ---------- Type Select ----------

    @discord.ui.select(
        placeholder="Step 2: Select Type",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label=t, value=t) for t in CAKE_TYPES
        ],
        row=1,
    )
    async def select_type(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This menu is not for you.", ephemeral=True
            )
            return

        if not self.selected_size:
            await interaction.response.send_message(
                "Please select **Size** first.", ephemeral=True
            )
            return

        self.selected_type = select.values[0]
        self.selected_product = None

        await interaction.response.defer()
        await self.update_message(interaction)

    # ---------- Product Select (dynamic) ----------

    @discord.ui.select(
        placeholder="Step 3: Select Product",
        min_values=1,
        max_values=1,
        options=[  # initial placeholder; real options set on first use
            discord.SelectOption(
                label="Select type first",
                value="__placeholder__",
                description="Choose Type before Product",
            )
        ],
        row=2,
    )
    async def select_product(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This menu is not for you.", ephemeral=True
            )
            return

        if not self.selected_type:
            await interaction.response.send_message(
                "Please select **Type** first.", ephemeral=True
            )
            return

        products = CAKE_PRODUCTS.get(self.selected_type, [])
        if not products:
            await interaction.response.send_message(
                "No products configured for this type yet.", ephemeral=True
            )
            return

        # If current options are still placeholder or do not match our list, rebuild them
        option_labels = {opt.label for opt in select.options}
        if not option_labels.issuperset(products):
            select.options = [
                discord.SelectOption(label=p, value=p) for p in products
            ]
            # Ask user to choose again from new options
            await interaction.response.defer()
            await self.update_message(interaction)
            return

        self.selected_product = select.values[0]

        await interaction.response.defer()
        await self.update_message(interaction)

    # ---------- Buttons ----------

    @discord.ui.button(
        label="Add to Cart",
        style=discord.ButtonStyle.green,
        row=3,
    )
    async def add_to_cart(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This button is not for you.", ephemeral=True
            )
            return

        if not (self.selected_size and self.selected_type and self.selected_product):
            await interaction.response.send_message(
                "Please select **Size**, **Type**, and **Product** first.",
                ephemeral=True,
            )
            return

        self.cart.append(
            (self.selected_size, self.selected_type, self.selected_product)
        )

        await interaction.response.defer()
        await self.update_message(interaction)

    @discord.ui.button(
        label="View Cart",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def view_cart(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This button is not for you.", ephemeral=True
            )
            return

        if not self.cart:
            await interaction.response.send_message(
                "🛒 Cart is empty.", ephemeral=True
            )
            return

        lines = ["🛒 Cart:"]
        for i, (s, t, p) in enumerate(self.cart, 1):
            lines.append(f"{i}. {s} / {t} / {p}")

        await interaction.response.send_message(
            "\n".join(lines), ephemeral=True
        )


# ---------- Public setup function ----------

def setup_cake_order_ui(bot: discord.Client | discord.ext.commands.Bot):
    """
    Call this from main.py AFTER creating `bot`
    to register the /cake_order command using the select-menu UI.
    """

    @bot.tree.command(
        name="cake_order",
        description="🎂 Interactive Cake Ordering System",
    )
    async def cake_order(interaction: discord.Interaction):
        view = CakeOrderView(interaction.user.id)

        embed = discord.Embed(
            title="🎂 Cake Order System",
            description=(
                "Size: (not selected)\n"
                "Type: (not selected)\n"
                "Product: (not selected)\n\n"
                "🛒 Cart: (empty)"
            ),
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=False,
        )
