from datetime import date
import polars as pl
from classes.cc.simplii_visa import SimpliiVisaStatement
from classes.db.generics.finance_db import FinanceDB
from classes.cc.rogers import RogersStatement
from classes.cc.ref_data import reimbursement_merchant_ref

class MyFinanceDB(FinanceDB):
    reimbursement_subcategory_id = 14

    def __init__(self, debug: bool = False):
        super().__init__(database_name="finance", debug=debug)

    def get_subcategory_id_from_name(self, subcategory_name: str) -> int:
        """
        Get the subcategory id from the subcategory name.
        """
        query = "select id from subcategories where name = %s"
        return self.select(query, (subcategory_name,))[0][0]

    def get_category_id_from_subcategory_id(self, subcategory_id: int) -> int:
        """
        Get the category id from the subcategory id.
        """
        query = "select category_id from subcategories where id = %s"
        return self.select(query, (subcategory_id,))[0][0]

    def get_category_and_subcategory_name_from_subcategory_id(self, subcategory_id: int) -> tuple[str, str]:
        """
        Get the category and subcategory name from the subcategory id.
        """
        query = "select c.name as category, s.name as subcategory from categories c join subcategories s on c.id = s.category_id where s.id = %s"
        return self.select(query, (subcategory_id,))[0]

    def get_subcategory_and_category(self) -> pl.DataFrame:
        """
        Get all subcategories and categories.
        """
        query = "select subcategories.id as subcategory_id, subcategories.name as subcategory, categories.name as category from subcategories join categories on subcategories.category_id = categories.id"
        schema = {
            "subcategory_id": pl.Int64,
            "subcategory": pl.Utf8,
            "category": pl.Utf8
        }
        return pl.DataFrame(self.select(query), schema=schema, orient="row")

    def check_if_reimbursement_expense_exists(self, date: date, merchant: str) -> bool:
        """
        Check if a reimbursement expense exists in the database.
        """
        return self._check_exists("expenses", {"date": date, "merchant": merchant})

    def insert_expense(self, date: date, merchant: str, cost: float, card_type: str, cc_category: str | None = None) -> None:
        print(f"Transaction on {date} at {merchant} for {cost}")
        category = None
        subcategory = None
        # try auto match
        found_match = False
        if card_type == "rogers" and cc_category is not None:
            # only rogers cc uses cc_category, so try ref rogers automatch first
            ref_category_tuple = RogersStatement.auto_match_category(cc_category)
            # then try get_auto_match_category
            if ref_category_tuple is None:
                ref_category_tuple = self.get_auto_match_category(merchant)
        elif card_type == "simplii_visa":
            ref_category_tuple = SimpliiVisaStatement.auto_match_category()
        else:
            # use merchant name to auto match
            ref_category_tuple = self.get_auto_match_category(merchant)
        # if auto match found get category id and subcategory id
        if ref_category_tuple is not None:
            found_match = True
            category, subcategory = ref_category_tuple
            print(f"Ref Category: {category}, Ref Subcategory: {subcategory}")
            subcategory_id = self.get_subcategory_id_from_name(subcategory)
            category_id = self.get_category_id_from_subcategory_id(subcategory_id)
        # else user input
        else:
            df = self.get_subcategory_and_category()
            print("\n\n")
            print(df)
            print("\n\n")
            subcategory_id = input("Enter the subcategory id: ")
            print(subcategory_id)
            subcategory_id = int(subcategory_id)
            category_id = self.get_category_id_from_subcategory_id(subcategory_id)
        # if subcategory is reimbursement or if in reimbursement_merchant_ref, need to double check if record already exists (date, merchant only)
        if any(merchant.lower() in reimbursement_merchant.lower() for reimbursement_merchant in reimbursement_merchant_ref) or subcategory_id == self.reimbursement_subcategory_id:
            if self.check_if_reimbursement_expense_exists(date, merchant):
                print(f"Record already exists for {date} at {merchant}. Skipping...")
                return
        # insert the expense
        query = "insert into expenses (date, merchant, cost, category_id, subcategory_id) values (%s, %s, %s, %s, %s)"
        self.insert(query, (date, merchant, cost, category_id, subcategory_id))
        # ask the user if they want to add the merchant to the auto_match table
        if not found_match:
            # if merchant is "Interac e-Transfer® Out", skip
            if merchant == "Interac e-Transfer® Out":
                return
            while True:
                add_to_auto_match = input("Add to auto_match table? (y/n): ")
                if add_to_auto_match == "y":
                    # check if category and subcategory are not None, if they are None, get the names from the database
                    if category is None or subcategory is None:
                        category, subcategory = self.get_category_and_subcategory_name_from_subcategory_id(subcategory_id)
                    self.insert_into_auto_match(merchant, category, subcategory)
                    break
                elif add_to_auto_match == "n":
                    break
                else:
                    print("Please enter a valid response (y/n).")

    def get_auto_match_category(self, merchant: str) -> tuple[str, str] | None:
        """
        Get the category and subcategory for the merchant.
        """
        query = "select merchant_category, merchant_subcategory from merchant_name_auto_match where merchant_name = %s"
        result = self.select(query, (merchant,))
        if len(result) > 1:
            raise ValueError(f"Multiple categories found for {merchant}. Something is wrong.")
        elif len(result) == 1:
            return result[0]
        else:
            # try substring auto match
            query = "select substring, merchant_category, merchant_subcategory from substring_auto_match"
            result = self.select(query)
            substring_matches = list()
            for item in result:
                if item[0] in merchant.lower():
                    substring_matches.append((item[1], item[2]))
            if len(substring_matches) > 1:
                raise ValueError(f"Multiple categories found for {merchant}. Something is wrong.")
            elif len(substring_matches) == 1:
                return substring_matches[0]
            else:
                return None

    def insert_into_auto_match(self, merchant: str, category: str, subcategory: str) -> None:
        """
        Insert a new merchant into the auto_match table.
        """
        query = "insert into merchant_name_auto_match (merchant_name, merchant_category, merchant_subcategory) values (%s, %s, %s)"
        self.insert(query, (merchant, category, subcategory))
